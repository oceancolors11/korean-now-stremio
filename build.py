#!/usr/bin/env python3

import os
import json
import math
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests
import numpy as np
import cv2
from PIL import Image


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


session = requests.Session()
session.headers.update({
    "Authorization": f"Bearer {TOKEN}",
    "accept": "application/json",
    "User-Agent": "KoreanNow-Stremio/1.2"
})


TODAY = datetime.now(ZoneInfo("Asia/Riyadh")).date()
MONTH_START = TODAY.replace(day=1)

# Keep a rolling window so shows that started shortly before
# this month but are still airing remain visible.
RECENT_START = TODAY - timedelta(days=35)
FUTURE_END = TODAY + timedelta(days=14)


# ============================================================
# TMDB helpers
# ============================================================

def tmdb(path, params=None, timeout=30):
    response = session.get(
        API + path,
        params=params or {},
        timeout=timeout
    )
    response.raise_for_status()
    return response.json()


def img(path, size):
    if not path:
        return None
    return f"{IMG}/{size}{path}"


def parse(d):
    try:
        return date.fromisoformat(d)
    except Exception:
        return None


# ============================================================
# Discovery
# ============================================================

def discover(params, pages=3):
    out = []

    for page in range(1, pages + 1):
        query = dict(params)
        query["page"] = page

        data = tmdb("/discover/tv", query)

        out.extend(data.get("results", []))

        if page >= data.get("total_pages", 1):
            break

        time.sleep(0.12)

    return out


# Two pools:
# 1) Shows with episode air dates around the current period
# 2) Shows whose first episode aired this month

pool = []

pool += discover({
    "language": "en-US",
    "with_original_language": "ko",
    "with_origin_country": "KR",
    "include_adult": "false",
    "include_null_first_air_dates": "false",
    "air_date.gte": str(RECENT_START),
    "air_date.lte": str(FUTURE_END),
    "sort_by": "popularity.desc",
    "vote_count.gte": "5"
}, pages=4)


pool += discover({
    "language": "en-US",
    "with_original_language": "ko",
    "with_origin_country": "KR",
    "include_adult": "false",
    "include_null_first_air_dates": "false",
    "first_air_date.gte": str(MONTH_START),
    "first_air_date.lte": str(TODAY),
    "sort_by": "popularity.desc",
    "vote_count.gte": "3"
}, pages=3)


# ============================================================
# De-duplicate
# ============================================================

by_id = {}

for item in pool:
    if item.get("id"):
        by_id[item["id"]] = item

candidates = list(by_id.values())


# ============================================================
# Show scoring
# ============================================================

def active_score(show):
    first = parse(show.get("first_air_date", ""))
    last = parse(show.get("last_air_date", ""))

    next_ep = show.get("next_episode_to_air")
    status = show.get("status", "")

    popularity = float(show.get("popularity") or 0)
    votes = int(show.get("vote_count") or 0)

    is_new = bool(
        first and
        MONTH_START <= first <= TODAY
    )

    is_recent = bool(
        first and
        first >= RECENT_START
    )

    airing = bool(next_ep) or (
        last and
        last >= TODAY - timedelta(days=21) and
        status in {
            "Returning Series",
            "In Production",
            "Planned"
        }
    )

    score = popularity

    if airing:
        score += 120

    if is_new:
        score += 80
    elif is_recent:
        score += 30

    score += min(votes, 10000) / 250

    return score


# ============================================================
# Fetch detailed metadata
# ============================================================

candidates.sort(
    key=active_score,
    reverse=True
)

selected = []


for basic in candidates[:60]:

    try:

        detail = tmdb(
            f"/tv/{basic['id']}",
            {
                "language": "en-US",
                "append_to_response": "images,videos,credits",
                "include_image_language": "ar,en,null,ko"
            }
        )

        status = detail.get("status", "")
        last = parse(detail.get("last_air_date", ""))
        first = parse(detail.get("first_air_date", ""))
        next_ep = detail.get("next_episode_to_air")

        airing = bool(next_ep) or (
            last and
            last >= TODAY - timedelta(days=21) and
            status in {
                "Returning Series",
                "In Production",
                "Planned"
            }
        )

        is_new = bool(
            first and
            MONTH_START <= first <= TODAY
        )

        is_recent = bool(
            first and
            first >= RECENT_START
        )

        if airing or is_new or is_recent:

            detail["_airing"] = airing
            detail["_is_new"] = is_new
            detail["_score"] = active_score(detail)

            # ------------------------------------------------
            # Arabic localized metadata
            # ------------------------------------------------

            try:
                arabic = tmdb(
                    f"/tv/{basic['id']}",
                    {
                        "language": "ar-SA"
                    }
                )

                detail["_arabic_name"] = (
                    arabic.get("name") or ""
                ).strip()

                detail["_arabic_overview"] = (
                    arabic.get("overview") or ""
                ).strip()

            except Exception as arabic_error:

                print(
                    "Arabic metadata failed",
                    basic.get("id"),
                    arabic_error
                )

                detail["_arabic_name"] = ""
                detail["_arabic_overview"] = ""

            selected.append(detail)

    except Exception as error:

        print(
            "detail failed",
            basic.get("id"),
            error
        )


selected.sort(
    key=lambda x: x["_score"],
    reverse=True
)

selected = selected[:36]


# ============================================================
# Visual Backdrop Analysis
# ============================================================

BACKDROP_CACHE = {}


def download_cv_image(url):
    """
    Download an image and decode it with OpenCV.
    """

    try:

        response = session.get(
            url,
            timeout=15
        )

        response.raise_for_status()

        raw = np.frombuffer(
            response.content,
            dtype=np.uint8
        )

        image = cv2.imdecode(
            raw,
            cv2.IMREAD_COLOR
        )

        if image is None:
            return None

        return image

    except Exception as error:

        print(
            "image download failed:",
            error
        )

        return None


def analyze_backdrop(url):
    """
    Visual safety analysis for Hero backdrops.

    Checks:
    - image quality
    - face positions
    - faces near edges
    - faces inside the title/text area
    - visual busyness in the title area
    - text-like regions
    """

    if url in BACKDROP_CACHE:
        return BACKDROP_CACHE[url]

    image = download_cv_image(url)

    if image is None:
        BACKDROP_CACHE[url] = None
        return None

    height, width = image.shape[:2]

    # --------------------------------------------------------
    # Basic quality check
    # --------------------------------------------------------

    if width < 1000 or height < 500:
        BACKDROP_CACHE[url] = None
        return None

    gray = cv2.cvtColor(
        image,
        cv2.COLOR_BGR2GRAY
    )

    score = 0.0

    # --------------------------------------------------------
    # Face detection
    # --------------------------------------------------------

    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades +
        "haarcascade_frontalface_default.xml"
    )

    faces = face_cascade.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(
            max(30, width // 25),
            max(30, height // 25)
        )
    )

    face_count = len(faces)

    # Slight preference for scenes without excessive
    # numbers of faces competing with the Hero text.
    if face_count > 5:
        score -= 8

    for x, y, fw, fh in faces:

        center_x = (x + fw / 2) / width
        center_y = (y + fh / 2) / height

        right_edge = width - (x + fw)
        bottom_edge = height - (y + fh)

        nearest_edge = min(
            x,
            y,
            right_edge,
            bottom_edge
        )

        # ----------------------------------------------------
        # Face touching the image edge
        # ----------------------------------------------------

        if nearest_edge < min(width, height) * 0.025:
            score -= 35

        # ----------------------------------------------------
        # Face too close to horizontal edges
        # ----------------------------------------------------

        if center_x < 0.12 or center_x > 0.88:
            score -= 30

        elif center_x < 0.20 or center_x > 0.80:
            score -= 12

        # ----------------------------------------------------
        # Face inside the likely Hero text area
        #
        # Nuvio's Hero information normally occupies the
        # left side of the composition.
        # ----------------------------------------------------

        if (
            0.10 <= center_x <= 0.62
            and
            0.20 <= center_y <= 0.82
        ):
            score -= 22

        # ----------------------------------------------------
        # Very large face near an edge
        # ----------------------------------------------------

        face_area = (
            fw * fh
        ) / float(width * height)

        if (
            face_area > 0.18
            and
            (center_x < 0.20 or center_x > 0.80)
        ):
            score -= 18

    # --------------------------------------------------------
    # Hero title/text zone
    # --------------------------------------------------------
    #
    # We intentionally analyze the left side because this is
    # where the Hero title, metadata and description normally
    # compete with the artwork.
    # --------------------------------------------------------

    zone_x2 = int(width * 0.58)
    zone_y1 = int(height * 0.20)
    zone_y2 = int(height * 0.78)

    zone = gray[
        zone_y1:zone_y2,
        :zone_x2
    ]

    if zone.size:

        # Resize for stable analysis.
        target_width = 320

        target_height = max(
            1,
            int(
                zone.shape[0] *
                target_width /
                zone.shape[1]
            )
        )

        small = cv2.resize(
            zone,
            (target_width, target_height)
        )

        # ----------------------------------------------------
        # Edge / detail density
        # ----------------------------------------------------

        gx = cv2.Sobel(
            small,
            cv2.CV_32F,
            1,
            0,
            ksize=3
        )

        gy = cv2.Sobel(
            small,
            cv2.CV_32F,
            0,
            1,
            ksize=3
        )

        magnitude = cv2.magnitude(
            gx,
            gy
        )

        edge_density = float(
            np.mean(magnitude > 70)
        )

        # Busy backgrounds make white/bright logos harder
        # to read.
        score -= min(
            edge_density * 160,
            24
        )

        # ----------------------------------------------------
        # Luminance variation
        # ----------------------------------------------------

        luminance_std = float(
            np.std(small)
        )

        if luminance_std > 55:
            score -= min(
                (luminance_std - 55) * 0.18,
                10
            )

        # ----------------------------------------------------
        # Text-like region detection
        #
        # This is intentionally only a soft penalty.
        # It helps reject artwork with large text-like
        # structures but does not pretend to be perfect OCR.
        # ----------------------------------------------------

        try:

            mser = cv2.MSER_create()

            regions, _ = mser.detectRegions(
                zone
            )

            text_like = 0

            for points in regions:

                point_count = len(points)

                if point_count < 8:
                    continue

                if point_count > 2000:
                    continue

                rx, ry, rw, rh = cv2.boundingRect(
                    points
                )

                if (
                    3 <= rw <= zone_x2 * 0.55
                    and
                    3 <= rh <= height * 0.20
                ):
                    text_like += 1

            score -= min(
                text_like * 0.35,
                12
            )

        except Exception:
            pass

    result = {
        "score": score,
        "faces": face_count,
        "edge_density": edge_density
        if zone.size
        else 0.0,
        "luminance_std": luminance_std
        if zone.size
        else 0.0,
    }

    BACKDROP_CACHE[url] = result

    return result


# ============================================================
# Backdrop selection
# ============================================================

def choose_backdrop(d):

    images = d.get("images") or {}

    backdrops = images.get("backdrops") or []

    # --------------------------------------------------------
    # Fallback if TMDB has no image list
    # --------------------------------------------------------

    if not backdrops:

        fallback = d.get("backdrop_path")

        if fallback:

            fallback_url = img(
                fallback,
                "w1280"
            )

            analysis = analyze_backdrop(
                fallback_url
            )

            if analysis is not None:
                return img(
                    fallback,
                    "w1920"
                )

            return fallback_url

        return None

    # --------------------------------------------------------
    # First filter:
    # Real Hero-like landscape images only.
    # --------------------------------------------------------

    usable = []

    for item in backdrops:

        path = item.get("file_path")

        if not path:
            continue

        aspect_ratio = float(
            item.get("aspect_ratio") or 0
        )

        width = int(
            item.get("width") or 0
        )

        height = int(
            item.get("height") or 0
        )

        if width < 1280:
            continue

        if height < 700:
            continue

        if not (
            1.68 <= aspect_ratio <= 1.86
        ):
            continue

        usable.append(item)

    # If strict filtering leaves nothing,
    # use the wider TMDB set.
    if not usable:

        usable = [
            x for x in backdrops
            if x.get("file_path")
        ]

    # --------------------------------------------------------
    # Prefer clean / language-neutral backdrops.
    #
    # TMDB says most backdrops do not have a language.
    # These are therefore preferred for a clean Hero.
    # --------------------------------------------------------

    neutral = [
        x for x in usable
        if x.get("iso_639_1") is None
    ]

    if neutral:
        usable = neutral

    # --------------------------------------------------------
    # Metadata score
    # --------------------------------------------------------

    def metadata_score(item):

        aspect_ratio = float(
            item.get("aspect_ratio") or 0
        )

        width = int(
            item.get("width") or 0
        )

        height = int(
            item.get("height") or 0
        )

        vote_average = float(
            item.get("vote_average") or 0
        )

        vote_count = int(
            item.get("vote_count") or 0
        )

        language = item.get("iso_639_1")

        score = 0.0

        # Quality
        if width >= 3840:
            score += 24
        elif width >= 2560:
            score += 18
        elif width >= 1920:
            score += 12
        elif width >= 1600:
            score += 6

        if height >= 2160:
            score += 10
        elif height >= 1440:
            score += 7
        elif height >= 1080:
            score += 5

        # Ideal cinematic ratio
        if 1.74 <= aspect_ratio <= 1.80:
            score += 18
        elif 1.70 <= aspect_ratio <= 1.83:
            score += 10

        # Community signal
        score += vote_average * 2.5
        score += min(vote_count, 100) * 0.05

        # Clean/no-language image
        if language is None:
            score += 18
        else:
            score -= 8

        return score

    usable.sort(
        key=metadata_score,
        reverse=True
    )

    # --------------------------------------------------------
    # Visually inspect only the best candidates.
    #
    # This avoids downloading every TMDB image.
    # --------------------------------------------------------

    candidates_to_check = usable[:7]

    best_item = None
    best_score = -10**9

    for item in candidates_to_check:

        path = item.get("file_path")

        if not path:
            continue

        analysis_url = img(
            path,
            "w1280"
        )

        visual = analyze_backdrop(
            analysis_url
        )

        base_score = metadata_score(
            item
        )

        if visual is None:

            # Do not completely reject it because a
            # temporary image download could fail.
            final_score = base_score - 18

        else:

            final_score = (
                base_score +
                visual["score"]
            )

            # Hard penalties for obvious Hero problems.

            if visual["faces"] > 0:

                # A little breathing room for faces.
                final_score += 2

            if visual["edge_density"] > 0.28:
                final_score -= 10

            if visual["edge_density"] > 0.35:
                final_score -= 15

            if visual["luminance_std"] > 80:
                final_score -= 6

        if final_score > best_score:

            best_score = final_score
            best_item = item

    # --------------------------------------------------------
    # Final fallback
    # --------------------------------------------------------

    if best_item and best_item.get("file_path"):

        return img(
            best_item["file_path"],
            "w1920"
        )

    if usable and usable[0].get("file_path"):

        return img(
            usable[0]["file_path"],
            "w1920"
        )

    return None


# ============================================================
# Logo selection
# ============================================================

def choose_logo(d):

    logos = (
        d.get("images") or {}
    ).get("logos") or []

    if not logos:
        return None

    def logo_score(item):

        language = item.get("iso_639_1")

        width = int(
            item.get("width") or 0
        )

        vote_average = float(
            item.get("vote_average") or 0
        )

        aspect_ratio = float(
            item.get("aspect_ratio") or 0
        )

        score = 0.0

        # ----------------------------------------------------
        # Language priority:
        #
        # Arabic > English > Korean > neutral
        # ----------------------------------------------------

        if language == "ar":
            score += 30
        elif language == "en":
            score += 20
        elif language == "ko":
            score += 10
        elif language is None:
            score += 5

        # Quality
        if width >= 1000:
            score += 10
        elif width >= 600:
            score += 6
        elif width >= 300:
            score += 3

        # Logos are usually landscape.
        if 1.5 <= aspect_ratio <= 8:
            score += 4

        score += vote_average * 2

        return score

    logos.sort(
        key=logo_score,
        reverse=True
    )

    best = logos[0]

    if best.get("file_path"):
        return img(
            best["file_path"],
            "w500"
        )

    return None


# ============================================================
# Metadata
# ============================================================

def meta(d):

    tid = d["id"]

    # --------------------------------------------------------
    # Title:
    #
    # Arabic -> English -> Original Korean
    # --------------------------------------------------------

    arabic_name = (
        d.get("_arabic_name") or ""
    ).strip()

    english_name = (
        d.get("name") or ""
    ).strip()

    original_name = (
        d.get("original_name") or ""
    ).strip()

    if arabic_name:
        display_name = arabic_name

    elif english_name:
        display_name = english_name

    elif original_name:
        display_name = original_name

    else:
        display_name = "Unknown"

    # --------------------------------------------------------
    # Description:
    #
    # Prefer Arabic when available.
    # Otherwise use English.
    # --------------------------------------------------------

    arabic_overview = (
        d.get("_arabic_overview") or ""
    ).strip()

    english_overview = (
        d.get("overview") or ""
    ).strip()

    description = (
        arabic_overview
        or english_overview
    )

    # --------------------------------------------------------
    # Release year
    # --------------------------------------------------------

    first_air_date = (
        d.get("first_air_date") or ""
    )

    release_info = ""

    if first_air_date:
        release_info = (
            first_air_date[:4] + "-"
        )

    # --------------------------------------------------------
    # Genres
    # --------------------------------------------------------

    genres = [
        genre.get("name")
        for genre in d.get("genres", [])
        if genre.get("name")
    ]

    # --------------------------------------------------------
    # Cast
    # --------------------------------------------------------

    cast = [
        person.get("name")
        for person in (
            d.get("credits") or {}
        ).get("cast", [])[:8]
        if person.get("name")
    ]

    # --------------------------------------------------------
    # Final Meta
    # --------------------------------------------------------

    return {
        "id": f"krtmdb_{tid}",

        "type": "series",

        "name": display_name,

        "poster": img(
            d.get("poster_path"),
            "w500"
        ),

        "posterShape": "poster",

        "background": choose_backdrop(d),

        "logo": choose_logo(d),

        "description": description,

        "releaseInfo": release_info,

        "imdbRating": (
            f"{float(d.get('vote_average', 0)):.1f}"
            if d.get("vote_average") is not None
            else None
        ),

        "genres": genres,

        "cast": cast,
    }


# ============================================================
# Build metas
# ============================================================

previews = []
metas = {}


for show in selected:

    metadata = meta(show)

    metas[show["id"]] = metadata

    previews.append({
        "id": metadata["id"],
        "type": "series",
        "name": metadata["name"],
        "poster": metadata["poster"],
        "posterShape": "poster"
    })


# ============================================================
# Ensure directories
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
# Clean old generated files
# ============================================================

for file in (
    SITE / "catalog/series"
).glob("korean_now*.json"):

    file.unlink()


for file in (
    SITE / "meta/series"
).glob("krtmdb_*.json"):

    file.unlink()


# ============================================================
# Main catalog
# ============================================================

for skip in range(
    0,
    len(previews),
    20
):

    chunk = previews[
        skip:skip + 20
    ]

    filename = (
        "korean_now.json"
        if skip == 0
        else f"skip={skip}.json"
    )

    (
        SITE / "catalog/series" / filename
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
# Individual Meta files
# ============================================================

for tid, metadata in metas.items():

    (
        SITE / "meta/series" /
        f"krtmdb_{tid}.json"
    ).write_text(
        json.dumps(
            {
                "meta": metadata
            },
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# Manifest
# ============================================================

(
    SITE / "manifest.json"
).write_text(
    json.dumps(
        {
            "id": "com.korean.now",

            "version": "1.2.0",

            "name": "Korean Now",

            "description": (
                "Current and newly released Korean TV series, "
                "with TMDB backdrops and multilingual logos "
                "optimized for Stremio/Nuvio."
            ),

            "resources": [
                "catalog",
                "meta"
            ],

            "types": [
                "series"
            ],

            "idPrefixes": [
                "krtmdb_"
            ],

            "catalogs": [
                {
                    "type": "series",
                    "id": "korean_now",
                    "name": "🇰🇷 Korean Now",
                    "extra": [
                        {
                            "name": "skip",
                            "isRequired": False
                        }
                    ]
                }
            ],

            "logo": (
                "https://www.themoviedb.org/"
                "assets/2/v4/logos/"
                "stacked-blue-square-400.svg"
            )
        },
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
<meta charset="utf-8">
<title>Korean Now</title>

<h1>Korean Now</h1>

<p>
This product uses the TMDB API but is not endorsed or certified by TMDB.
</p>

<p>
Data and images are provided by The Movie Database (TMDB).
</p>
""",
    encoding="utf-8"
)


# ============================================================
# Done
# ============================================================

print(
    f"Built {len(previews)} Korean series for {TODAY}"
)

print(
    "Backdrop visual analysis:",
    len(BACKDROP_CACHE),
    "images checked"
)
