"""Fit exponential variogram models for the kriging surface.

Pools hourly-mean sensor values from the archive, standardizes each hour
across the network (so smoke events contribute correlation structure, not
raw variance), computes an empirical semivariogram, and fits
    gamma(d) = nugget + psill * (1 - exp(-3 d / range))
by weighted least squares over a coarse parameter grid. Ordinary-kriging
weights are invariant to the variogram's overall scale, so standardized
fitting is valid for the map surface; the (relative) kriging variance drives
the overlay's opacity fade.

Writes data/variogram.json. Run weekly in CI alongside fit_calibration.py.
"""

import math
import statistics
from collections import defaultdict

from common import (
    DATA_DIR,
    SENSORS_PATH,
    SPECIES,
    haversine_m,
    iso,
    load_config,
    load_json,
    parse_iso,
    read_archive,
    species_values,
    utcnow,
    write_json,
)

VARIOGRAM_PATH = DATA_DIR / "variogram.json"

BIN_M = 1000
MAX_DIST_M = 15000
MIN_STATIONS_PER_HOUR = 6
MIN_PAIRS_TOTAL = 300

SEED = {"model": "exponential", "nugget": 0.15, "psill": 0.85, "range_m": 4000}


def default_variogram(note="seed defaults"):
    out = {"fitted_at": None, "note": note}
    for sp in SPECIES:
        out[sp] = dict(SEED)
    return out


def hourly_standardized(rows, sensors):
    """-> {species: {hour: {sensor_id: z}}} using base values."""
    acc = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for row in rows:
        sid = row["id"]
        if sid not in sensors or sensors[sid].get("lat") is None:
            continue
        hour = iso(parse_iso(row["ts"]).replace(minute=0, second=0))
        for sp, v in species_values(row).items():
            acc[sp][hour][sid].append(v)

    out = {}
    for sp, hours in acc.items():
        out[sp] = {}
        for hour, per_sensor in hours.items():
            means = {sid: sum(vs) / len(vs) for sid, vs in per_sensor.items()}
            if len(means) < MIN_STATIONS_PER_HOUR:
                continue
            mu = statistics.fmean(means.values())
            sd = statistics.pstdev(means.values())
            if sd < 0.5:  # spatially flat hour carries no structure signal
                continue
            out[sp][hour] = {sid: (v - mu) / sd for sid, v in means.items()}
    return out


def empirical_variogram(z_by_hour, sensors):
    """Matheron estimator on pooled standardized pairs. -> [(d, gamma, n)]"""
    nbins = MAX_DIST_M // BIN_M
    sums = [0.0] * nbins
    counts = [0] * nbins
    dist_cache = {}
    for per_sensor in z_by_hour.values():
        sids = sorted(per_sensor)
        for i in range(len(sids)):
            for j in range(i + 1, len(sids)):
                key = (sids[i], sids[j])
                d = dist_cache.get(key)
                if d is None:
                    a, b = sensors[sids[i]], sensors[sids[j]]
                    d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
                    dist_cache[key] = d
                b_i = int(d // BIN_M)
                if b_i >= nbins:
                    continue
                diff = per_sensor[sids[i]] - per_sensor[sids[j]]
                sums[b_i] += 0.5 * diff * diff
                counts[b_i] += 1
    return [
        ((k + 0.5) * BIN_M, sums[k] / counts[k], counts[k])
        for k in range(nbins)
        if counts[k] > 0
    ]


def fit_exponential(emp):
    """Weighted-LS grid search. emp: [(d, gamma, n)] -> (nugget, psill, range_m, sse)"""
    sill_obs = max(g for _, g, _ in emp)
    best = None
    for range_m in range(1000, 15001, 500):
        for nugget_frac in [0, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5]:
            for sill in [sill_obs * s for s in (0.7, 0.85, 1.0, 1.15, 1.3)]:
                nugget = nugget_frac * sill
                psill = sill - nugget
                sse = sum(
                    n * (g - (nugget + psill * (1 - math.exp(-3 * d / range_m)))) ** 2
                    for d, g, n in emp
                )
                if best is None or sse < best[3]:
                    best = (nugget, psill, range_m, sse)
    return best


def main():
    cfg = load_config()
    sensors = (load_json(SENSORS_PATH, default={}) or {}).get("sensors", {})
    rows = read_archive(max_age_days=cfg["archive"]["keep_days"])
    if not rows or not sensors:
        print("No archive/sensors yet; writing seed variogram.")
        write_json(VARIOGRAM_PATH, default_variogram())
        return

    z = hourly_standardized(rows, sensors)
    out = default_variogram(note="exponential fit on per-hour standardized values")
    out["fitted_at"] = iso(utcnow())
    for sp in SPECIES:
        emp = empirical_variogram(z.get(sp, {}), sensors)
        n_pairs = sum(n for _, _, n in emp)
        if n_pairs < MIN_PAIRS_TOTAL:
            print(f"{sp}: only {n_pairs} pairs; keeping seed defaults")
            out[sp]["n_pairs"] = n_pairs
            continue
        nugget, psill, range_m, _ = fit_exponential(emp)
        # degenerate fits (pure nugget or absurd range) fall back to seed
        if psill <= 0 or nugget / (nugget + psill) > 0.8:
            print(f"{sp}: degenerate fit (nugget-dominated); keeping seed defaults")
            out[sp]["n_pairs"] = n_pairs
            continue
        out[sp] = {
            "model": "exponential",
            "nugget": round(nugget, 4),
            "psill": round(psill, 4),
            "range_m": range_m,
            "n_pairs": n_pairs,
        }
        print(
            f"{sp}: nugget={nugget:.3f} psill={psill:.3f} range={range_m} m "
            f"(n_pairs={n_pairs}, {len(z.get(sp, {}))} hours)"
        )
    write_json(VARIOGRAM_PATH, out)
    print(f"Wrote {VARIOGRAM_PATH}")


if __name__ == "__main__":
    main()
