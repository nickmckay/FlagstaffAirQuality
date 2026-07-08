# Flagstaff Air Quality

Animated map of particulate matter (PM1.0, PM2.5, PM10) around Flagstaff,
Arizona, combining the [PurpleAir](https://www2.purpleair.com/) and
[Air Quality Egg](https://airqualityegg.com/) sensor networks. Updated every
15 minutes by GitHub Actions and served from GitHub Pages.

**Live site:** https://nickmckay.github.io/FlagstaffAirQuality/

## How it works

- `scripts/fetch_data.py` runs every 15 minutes (`.github/workflows/update-data.yml`):
  one bounding-box call to the PurpleAir API plus one call to the public
  Air Quality Egg `/eggs/mapped` feed (which carries each mapped Egg's latest
  readings with their own timestamps; Eggs publish sporadically, so readings
  up to 24 h old are archived into their true time slot). Results accumulate
  in a rolling 30-day archive and are condensed into animation frames (24 h
  and 48 h at 15-minute steps, 7 days hourly).
- Generated JSON lives on the single-commit `data` branch (force-pushed each
  run, so the repo history stays small) and is deployed to Pages together with
  the static site.
- The frontend (`index.html`, `js/app.js`) is a Leaflet map with a time
  slider, species and window toggles, colored sensor dots, and an
  inverse-distance-weighted surface clipped to ~3 km around reporting sensors.

## Calibration

PurpleAir sensors report raw PM but overestimate PM2.5, especially in smoke.
Two corrections are applied:

1. **EPA correction** (Barkjohn et al. 2021) for PM2.5, using the sensors'
   `pm2.5_cf_1` and relative humidity (with the quadratic high-concentration
   branch and a linear blend between 210 and 343 µg/m³).
2. **Local fit**: `scripts/fit_calibration.py` runs weekly, pairing PurpleAir
   sensors with Air Quality Eggs within 2 km on hourly averages (falling back
   to network-median regression) and fitting a robust Theil–Sen line per
   species. Coefficients are only applied when the fit passes sanity checks
   (n ≥ 100, slope in [0.2, 5], R² ≥ 0.3); otherwise raw/EPA values are used.

The archive always stores uncalibrated base values; the local fit is applied
when frames are built, so a refit retroactively updates the displayed history.

## Interpolated surface

The map overlay is ordinary kriging computed client-side per frame.
`scripts/fit_variogram.py` (weekly) pools hourly sensor values, standardizes
each hour across the network, and fits an exponential variogram per species;
the frontend solves the kriging system for the reporting sensors each frame
and shades the whole town, fading opacity where the kriging variance grows
(far outside the network) rather than by raw distance.

## Setup

1. **PurpleAir key**: create an account at
   [develop.purpleair.com](https://develop.purpleair.com) and make a **read**
   key. New accounts get 1M points; this pipeline uses roughly 0.5–1M/month.
   The fetch log prints points consumed per call; if the budget gets tight,
   trim `purpleair.data_fields` in `config.yaml` or slow the cron.
2. Add it as a repository secret: `gh secret set PURPLEAIR_API_KEY`.
   (Air Quality Egg data comes from the public mapped feed; no key needed.
   The `AQE_API_KEY` secret is optional and currently unused; the restricted
   per-device Egg endpoints only authorize eggs your account owns or follows.)
3. **Find Eggs**: `python scripts/discover_eggs.py`, then paste the reported
   serials (with lat/lon) into `config.yaml` under `airqualityegg.serials`.
4. Enable Pages with source "GitHub Actions" (Settings → Pages), then push or
   run the "Update air quality data" workflow manually.

## Local development

```bash
pip install -r requirements.txt
python scripts/fetch_data.py --synthetic   # 7 days of fake data, no keys needed
python -m http.server 8000                 # open http://localhost:8000
```

With keys in the environment, `python scripts/fetch_data.py` does a live pull;
`--frames-only` rebuilds frames from the archive without an API call.

## Notes

- GitHub cron can lag a few minutes at busy times; scheduled workflows are
  disabled after 60 days without repository activity (a push re-enables them).
- Data © PurpleAir, Inc. and Air Quality Egg / Wicked Device contributors;
  see their terms for reuse. Basemap © OpenStreetMap contributors, tiles by
  CARTO.
