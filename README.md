# Korean Now — Stremio Catalog

A free, static Stremio catalog generated daily from TMDB. It focuses on:
- Korean (`ko`) TV series
- currently airing / recently active shows
- shows that started during the current month
- recent launches from the previous ~35 days
- TMDB posters + high-quality backdrops + logos for the Stremio detail/Hero area

## Important
This does **not** provide streams. It is a catalog + metadata add-on. Your existing streaming add-ons remain responsible for playback.

## Free deployment with GitHub Pages
1. Create a free GitHub account if needed.
2. Create a new **public** repository, e.g. `korean-now-stremio`.
3. Upload all files from this folder to the repository, keeping the `.github/workflows/deploy.yml` path.
4. In GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `TMDB_TOKEN`
   - Value: your TMDB **API Read Access Token** (the long Bearer token, not the short API key).
   TMDB documents API credentials here: https://developer.themoviedb.org/docs/getting-started
5. Go to **Settings → Pages** and choose **GitHub Actions** as the source if GitHub asks.
6. Open **Actions → Build and deploy Korean Now → Run workflow** once.
7. Your manifest will be:
   `https://YOUR-USERNAME.github.io/YOUR-REPO/manifest.json`
8. Paste that manifest URL into Stremio's Add-ons page.

The workflow also runs every day, so the catalog refreshes automatically.

## How the Hero artwork is handled
The catalog returns posters. When Stremio/Nuvio requests full metadata, the generated meta includes:
- `background`: a selected TMDB backdrop
- `logo`: a selected TMDB logo when available
- `poster`: the poster

Backdrops are selected using aspect ratio, resolution, TMDB image rating/vote count and language-neutral artwork preferences. This cannot guarantee Nuvio's own Hero layout will always place text in an ideal empty area, but it gives Nuvio the complete artwork metadata instead of relying on a missing/third-party background.

## TMDB notice
This product uses the TMDB API but is not endorsed or certified by TMDB.
