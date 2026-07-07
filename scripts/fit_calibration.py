"""Fit local PurpleAir-vs-Egg calibration from the archived snapshots.

Pairs each PurpleAir sensor with Eggs within pair_max_distance_m on hourly
averages; if too few pairs exist, falls back to regressing the hourly
network median of PurpleAir against the hourly network median of the Eggs
(reasonable in a small town where smoke events are regional).

Fits a robust Theil-Sen line per species mapping PurpleAir -> Egg scale and
writes data/calibration.json. Coefficients are only marked apply=true when
the fit is well-behaved (enough points, sane slope, decent correlation).

Run weekly in CI once the archive has accumulated paired data.
"""

import statistics
from collections import defaultdict

from common import (
    CALIBRATION_PATH,
    SENSORS_PATH,
    SPECIES,
    default_calibration,
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

MIN_SLOPE, MAX_SLOPE = 0.2, 5.0
MIN_R2 = 0.3


def hourly_means(rows):
    """-> {(hour_iso, sensor_id): {species: mean}}"""
    acc = defaultdict(lambda: defaultdict(list))
    for row in rows:
        hour = parse_iso(row["ts"]).replace(minute=0, second=0)
        # base values (EPA-corrected PM2.5, raw PM1/PM10), never the local fit,
        # so refits regress against stable inputs
        for sp, v in species_values(row).items():
            acc[(iso(hour), row["id"])][sp].append(v)
    return {
        k: {sp: sum(vs) / len(vs) for sp, vs in d.items()} for k, d in acc.items()
    }


def theil_sen(points):
    """Robust line fit. points: [(x, y)]. Returns (slope, intercept, r2)."""
    n = len(points)
    slopes = []
    step = max(1, n * (n - 1) // 2 // 20000)  # cap pairwise slopes at ~20k
    k = 0
    for i in range(n):
        for j in range(i + 1, n):
            k += 1
            if k % step:
                continue
            dx = points[j][0] - points[i][0]
            if abs(dx) < 1e-9:
                continue
            slopes.append((points[j][1] - points[i][1]) / dx)
    if not slopes:
        return None
    slope = statistics.median(slopes)
    intercept = statistics.median(y - slope * x for x, y in points)
    ybar = statistics.fmean(y for _, y in points)
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in points)
    ss_tot = sum((y - ybar) ** 2 for _, y in points)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, intercept, r2


def collect_pairs(cfg, hourly, sensors):
    """Nearest-Egg pairing within pair_max_distance_m. -> {species: [(pa, egg)]}"""
    max_d = cfg["calibration"]["pair_max_distance_m"]
    pa_ids = [s for s, m in sensors.items() if m["network"] == "purpleair"]
    egg_ids = [s for s, m in sensors.items() if m["network"] == "egg"]
    partners = {}
    for pid in pa_ids:
        pm = sensors[pid]
        if pm.get("lat") is None:
            continue
        best, best_d = None, max_d
        for eid in egg_ids:
            em = sensors[eid]
            if em.get("lat") is None:
                continue
            d = haversine_m(pm["lat"], pm["lon"], em["lat"], em["lon"])
            if d <= best_d:
                best, best_d = eid, d
        if best:
            partners[pid] = best

    pairs = {sp: [] for sp in SPECIES}
    by_hour = defaultdict(dict)
    for (hour, sid), vals in hourly.items():
        by_hour[hour][sid] = vals
    for hour, sensors_at_hour in by_hour.items():
        for pid, eid in partners.items():
            pv, ev = sensors_at_hour.get(pid), sensors_at_hour.get(eid)
            if not pv or not ev:
                continue
            for sp in SPECIES:
                if sp in pv and sp in ev:
                    pairs[sp].append((pv[sp], ev[sp]))
    return pairs, partners


def collect_median_pairs(hourly, sensors):
    """Fallback: hourly network median PA vs network median Egg."""
    by_hour = defaultdict(lambda: {"purpleair": defaultdict(list), "egg": defaultdict(list)})
    for (hour, sid), vals in hourly.items():
        net = sensors.get(sid, {}).get("network")
        if net not in ("purpleair", "egg"):
            continue
        for sp, v in vals.items():
            by_hour[hour][net][sp].append(v)
    pairs = {sp: [] for sp in SPECIES}
    for hour, nets in by_hour.items():
        for sp in SPECIES:
            pa, egg = nets["purpleair"][sp], nets["egg"][sp]
            if pa and egg:
                pairs[sp].append((statistics.median(pa), statistics.median(egg)))
    return pairs


def main():
    cfg = load_config()
    sensors = (load_json(SENSORS_PATH, default={}) or {}).get("sensors", {})
    rows = read_archive(max_age_days=cfg["archive"]["keep_days"])
    if not rows or not sensors:
        print("No archive or sensor metadata yet; writing seed calibration.")
        write_json(CALIBRATION_PATH, default_calibration())
        return

    hourly = hourly_means(rows)
    min_pairs = cfg["calibration"]["min_pairs"]
    pairs, partners = collect_pairs(cfg, hourly, sensors)
    method = f"nearest-egg pairs within {cfg['calibration']['pair_max_distance_m']} m"
    if all(len(pairs[sp]) < min_pairs for sp in SPECIES):
        pairs = collect_median_pairs(hourly, sensors)
        method = "hourly network-median regression"

    out = default_calibration()
    out["fitted_at"] = iso(utcnow())
    out["method"] = method
    out["note"] = "maps calibrated PurpleAir values onto the Egg scale"
    if partners:
        out["pa_egg_partners"] = partners
    for sp in SPECIES:
        pts = pairs[sp]
        out[sp]["n"] = len(pts)
        if len(pts) < min_pairs:
            print(f"{sp}: only {len(pts)} pairs (<{min_pairs}); keeping identity")
            continue
        fit = theil_sen(pts)
        if fit is None:
            continue
        slope, intercept, r2 = fit
        out[sp].update(
            {"slope": round(slope, 4), "intercept": round(intercept, 3), "r2": round(r2, 3)}
        )
        ok = MIN_SLOPE <= slope <= MAX_SLOPE and r2 >= MIN_R2
        out[sp]["apply"] = ok
        print(
            f"{sp}: n={len(pts)} slope={slope:.3f} intercept={intercept:.2f} "
            f"r2={r2:.3f} -> {'APPLY' if ok else 'identity (fit rejected)'}"
        )

    write_json(CALIBRATION_PATH, out)
    print(f"Wrote {CALIBRATION_PATH} ({method})")


if __name__ == "__main__":
    main()
