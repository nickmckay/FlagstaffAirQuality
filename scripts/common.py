"""Shared helpers for the Flagstaff Air Quality data pipeline."""

import json
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
ARCHIVE_PATH = DATA_DIR / "archive.ndjson"
SENSORS_PATH = DATA_DIR / "sensors.json"
CALIBRATION_PATH = DATA_DIR / "calibration.json"

SPECIES = ["pm1", "pm25", "pm10"]


def load_config():
    with open(REPO_ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def floor_to_slot(dt, step_minutes):
    minutes = (dt.hour * 60 + dt.minute) // step_minutes * step_minutes
    return dt.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def epa_correction_pm25(cf1, rh):
    """EPA (Barkjohn et al. 2021) correction for PurpleAir PM2.5.

    Linear form below 210 ug/m3, quadratic form above 343 (wildfire smoke),
    linear blend between the two in the 210-343 transition zone.
    """
    if cf1 is None:
        return None
    if rh is None:
        rh = 50.0
    low = 0.524 * cf1 - 0.0862 * rh + 5.75
    if cf1 <= 210:
        out = low
    else:
        high = 0.46 * cf1 + 3.93e-4 * cf1**2 + 2.97
        if cf1 >= 343:
            out = high
        else:
            w = (cf1 - 210) / (343 - 210)
            out = (1 - w) * low + w * high
    return max(out, 0.0)


def apply_local_fit(value, fit):
    """Apply a fitted slope/intercept from calibration.json, if usable."""
    if value is None or not fit or not fit.get("apply"):
        return value
    return max(fit["slope"] * value + fit["intercept"], 0.0)


def species_values(row, calibration=None):
    """Derive pm1/pm25/pm10 for an archive row.

    Egg rows (and synthetic rows) store final values directly. PurpleAir rows
    store raw fields; PM2.5 gets the EPA correction, and the local fit from
    calibration.json is applied last (frames only, never re-archived, so
    refitting the calibration retroactively updates the whole history).
    """
    out = {}
    is_pa = row.get("network") == "purpleair"
    for sp in SPECIES:
        if isinstance(row.get(sp), (int, float)):
            out[sp] = row[sp]
        elif is_pa:
            if sp == "pm25":
                v = epa_correction_pm25(row.get("pm25_cf1"), row.get("rh"))
            else:
                v = row.get(f"{sp}_raw")
            if isinstance(v, (int, float)):
                if calibration:
                    v = apply_local_fit(v, calibration.get(sp))
                out[sp] = v
    return out


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def read_archive(max_age_days=None):
    """Yield archive rows: {ts, id, network, pm1, pm25, pm10, ...}."""
    if not ARCHIVE_PATH.exists():
        return []
    cutoff = None
    if max_age_days is not None:
        cutoff = utcnow() - timedelta(days=max_age_days)
    rows = []
    with open(ARCHIVE_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if cutoff and parse_iso(row["ts"]) < cutoff:
                continue
            rows.append(row)
    return rows


def append_archive(rows, keep_days):
    """Append rows, then rewrite the file pruned to keep_days.

    Deduplicates on (ts, id) with newest write winning — egg readings carry
    their own timestamps and get re-seen across polls.
    """
    merged = {}
    for row in read_archive(max_age_days=keep_days) + list(rows):
        merged[(row["ts"], row["id"])] = row
    existing = sorted(merged.values(), key=lambda r: r["ts"])
    ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = ARCHIVE_PATH.with_suffix(".tmp")
    with open(tmp, "w") as f:
        for row in existing:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    os.replace(tmp, ARCHIVE_PATH)


def default_calibration():
    return {
        "fitted_at": None,
        "note": "seed calibration: EPA correction for PM2.5, identity for PM1/PM10",
        "pm1": {"apply": False, "slope": 1.0, "intercept": 0.0},
        "pm25": {"apply": False, "slope": 1.0, "intercept": 0.0},
        "pm10": {"apply": False, "slope": 1.0, "intercept": 0.0},
    }
