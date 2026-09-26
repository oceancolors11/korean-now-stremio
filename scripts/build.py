#!/usr/bin/env python3
import os, json, math, time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import requests

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
    "User-Agent": "KoreanNow-Stremio/1.0"
})

TODAY = datetime.now(ZoneInfo("Asia/Riyadh")).date()
MONTH_START = TODAY.replace(day=1)
# Keep a rolling window so shows that started shortly before this month but are still airing remain visible.
RECENT_START = TODAY - timedelta(days=35)
FUTURE_END = TODAY + timedelta(days=14)

def tmdb(path, params=None):
    r = session.get(API + path, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()

def img(path, size):
    return f"{IMG}/{size}{path}" if path else None

def discover(params, pages=3):
    out=[]
    for p in range(1,pages+1):
        q=dict(params); q["page"]=p
        data=tmdb("/discover/tv", q)
        out.extend(data.get("results",[]))
        if p >= data.get("total_pages",1): break
        time.sleep(0.12)
    return out

# Two pools:
# 1) shows with episode air dates around the current period
# 2) shows whose first episode aired this month
pool = []
pool += discover({
    "language":"en-US",
    "with_original_language":"ko",
    "with_origin_country":"KR",
    "include_adult":"false",
    "include_null_first_air_dates":"false",
    "air_date.gte":str(RECENT_START),
    "air_date.lte":str(FUTURE_END),
    "sort_by":"popularity.desc",
    "vote_count.gte":"5"
}, pages=4)
pool += discover({
    "language":"en-US",
    "with_original_language":"ko",
    "with_origin_country":"KR",
    "include_adult":"false",
    "include_null_first_air_dates":"false",
    "first_air_date.gte":str(MONTH_START),
    "first_air_date.lte":str(TODAY),
    "sort_by":"popularity.desc",
    "vote_count.gte":"3"
}, pages=3)

# De-duplicate.
by_id={}
for x in pool:
    by_id[x["id"]]=x
candidates=list(by_id.values())

def parse(d):
    try: return date.fromisoformat(d)
    except: return None

def active_score(show):
    first=parse(show.get("first_air_date",""))
    last=parse(show.get("last_air_date",""))
    next_ep=show.get("next_episode_to_air")
    status=show.get("status","")
    pop=float(show.get("popularity") or 0)
    votes=int(show.get("vote_count") or 0)

    is_new = first and MONTH_START <= first <= TODAY
    is_recent = first and first >= RECENT_START
    airing = bool(next_ep) or (last and last >= TODAY-timedelta(days=21) and status in {"Returning Series","In Production","Planned"})
    # Favor currently active shows, then this month's launches, then recent launches.
    s = pop
    if airing: s += 120
    if is_new: s += 80
    elif is_recent: s += 30
    s += min(votes,10000)/250
    return s

# Fetch detailed metadata for enough candidates to make a good, stable list.
candidates.sort(key=active_score, reverse=True)
selected=[]
for basic in candidates[:60]:
    try:
        detail=tmdb(f"/tv/{basic['id']}", {
            "language":"en-US",
            "append_to_response":"images,videos",
            "include_image_language":"en,null,ko"
        })
        status=detail.get("status","")
        last=parse(detail.get("last_air_date",""))
        first=parse(detail.get("first_air_date",""))
        next_ep=detail.get("next_episode_to_air")
        airing = bool(next_ep) or (last and last >= TODAY-timedelta(days=21) and status in {"Returning Series","In Production","Planned"})
        is_new = bool(first and MONTH_START <= first <= TODAY)
        is_recent = bool(first and first >= RECENT_START)
        # Only keep relevant current/recent titles.
        if airing or is_new or is_recent:
            detail["_airing"]=airing
            detail["_is_new"]=is_new
            detail["_score"]=active_score(detail)
            selected.append(detail)
    except Exception as e:
        print("detail failed", basic.get("id"), e)

selected.sort(key=lambda x:x["_score"], reverse=True)
selected=selected[:36]

def choose_backdrop(d):
    bgs=(d.get("images") or {}).get("backdrops") or []
    if not bgs and d.get("backdrop_path"):
        return img(d["backdrop_path"], "w1280")
    # Prefer 16:9, high resolution, high community rating and enough votes.
    def score(x):
        ar=float(x.get("aspect_ratio") or 0)
        w=int(x.get("width") or 0)
        vr=float(x.get("vote_average") or 0)
        vc=int(x.get("vote_count") or 0)
        lang=x.get("iso_639_1")
        s=vr*8 + min(vc,50)*0.05
        if 1.70 <= ar <= 1.82: s += 12
        if w >= 1920: s += 8
        if lang is None: s += 4
        return s
    bgs=sorted(bgs,key=score,reverse=True)
    return img(bgs[0]["file_path"],"w1920") if bgs else None

def choose_logo(d):
    logos=(d.get("images") or {}).get("logos") or []
    if not logos: return None
    def score(x):
        lang=x.get("iso_639_1")
        w=int(x.get("width") or 0)
        vr=float(x.get("vote_average") or 0)
        s=vr*6 + min(w,2000)/500
        if lang=="en": s+=8
        if lang=="ko": s+=6
        if lang is None: s+=2
        return s
    logos=sorted(logos,key=score,reverse=True)
    return img(logos[0]["file_path"],"w500") if logos[0].get("file_path") else None

def meta(d):
    tid=d["id"]
    return {
        "id":f"krtmdb_{tid}",
        "type":"series",
        "name":d.get("name") or d.get("original_name") or "Unknown",
        "poster":img(d.get("poster_path"),"w500"),
        "posterShape":"poster",
        "background":choose_backdrop(d),
        "logo":choose_logo(d),
        "description":d.get("overview") or "",
        "releaseInfo":(str(d.get("first_air_date","")[:4])+"-" if d.get("first_air_date") else ""),
        "imdbRating":f"{float(d.get('vote_average',0)):.1f}" if d.get("vote_average") is not None else None,
        "genres":[g.get("name") for g in d.get("genres",[]) if g.get("name")],
        "cast":[c.get("name") for c in (d.get("credits") or {}).get("cast",[])[:8] if c.get("name")],
    }

previews=[]
metas={}
for d in selected:
    m=meta(d)
    metas[d["id"]]=m
    previews.append({
        "id":m["id"], "type":"series", "name":m["name"],
        "poster":m["poster"], "posterShape":"poster"
    })

# Ensure directories
(SITE/"catalog/series").mkdir(parents=True,exist_ok=True)
(SITE/"meta/series").mkdir(parents=True,exist_ok=True)

# Main catalog and static pagination pages.
for f in (SITE/"catalog/series").glob("korean_now*.json"):
    f.unlink()
for f in (SITE/"meta/series").glob("krtmdb_*.json"):
    f.unlink()

# Main feed: first 36. Stremio uses preview objects here.
for skip in range(0, len(previews), 20):
    chunk=previews[skip:skip+20]
    filename="korean_now.json" if skip==0 else f"skip={skip}.json"
    (SITE/"catalog/series"/filename).write_text(json.dumps({"metas":chunk},ensure_ascii=False),encoding="utf-8")

for tid,m in metas.items():
    (SITE/"meta/series"/f"krtmdb_{tid}.json").write_text(json.dumps({"meta":m},ensure_ascii=False),encoding="utf-8")

# Manifest copy.
(SITE/"manifest.json").write_text(json.dumps({
  "id":"com.korean.now",
  "version":"1.0.0",
  "name":"Korean Now",
  "description":"Current and newly released Korean TV series, with TMDB backdrops and logos optimized for Stremio/Nuvio.",
  "resources":["catalog","meta"],
  "types":["series"],
  "idPrefixes":["krtmdb_"],
  "catalogs":[{"type":"series","id":"korean_now","name":"🇰🇷 Korean Now","extra":[{"name":"skip","isRequired":False}]}],
  "logo":"https://www.themoviedb.org/assets/2/v4/logos/stacked-blue-square-400.svg"
},ensure_ascii=False,indent=2),encoding="utf-8")

# TMDB attribution/notice page.
(SITE/"about.html").write_text("""<!doctype html><meta charset="utf-8"><title>Korean Now</title>
<h1>Korean Now</h1>
<p>This product uses the TMDB API but is not endorsed or certified by TMDB.</p>
<p>Data and images are provided by The Movie Database (TMDB).</p>
""",encoding="utf-8")

print(f"Built {len(previews)} Korean series for {TODAY}")
