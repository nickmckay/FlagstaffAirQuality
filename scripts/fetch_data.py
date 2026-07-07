"""Fetch current PM data from PurpleAir and Air Quality Egg, calibrate, and
regenerate the frames JSON consumed by the map.

Runs every 15 minutes in CI. Requires env vars:
  PURPLEAIR_API_KEY  - PurpleAir read key
  AQE_API_KEY        - Air Quality Egg API key (only needed if eggs configured)

Usage:
  python scripts/fetch_data.py                # live fetch + rebuild frames
  python scripts/fetch_data.py --synthetic    # generate 7 days of fake data
  python scripts/fetch_data.py --frames-only  # rebuild frames from archive
"""

import argparse
import math
import os
import random
import re
import sys
from datetime import timedelta

import requests

from common import (
    ARCHIVE_PATH,
    CALIBRATION_PATH,
    DATA_DIR,
    SENSORS_PATH,
    SPECIES,
    append_archive,
    default_calibration,
    floor_to_slot,
    iso,
    load_config,
    load_json,
    parse_iso,
    read_archive,
    species_values,
    utcnow,
    write_json,
)

METADATA_MAX_AGE_H = 24


def fetch_purpleair(cfg, sensors_meta):
    """One /v1/sensors bounding-box call. Returns (rows, updated_meta)."""
    key = os.environ.get("PURPLEAIR_API_KEY")
    if not key:
        print("WARNING: PURPLEAIR_API_KEY not set; skipping PurpleAir")
        return [], sensors_meta

    pa = cfg["purpleair"]
    bbox = cfg["region"]["bbox"]
    meta_fresh = False
    fetched_at = sensors_meta.get("purpleair_meta_fetched_at")
    if fetched_at:
        age_h = (utcnow() - parse_iso(fetched_at)).total_seconds() / 3600
        meta_fresh = age_h < METADATA_MAX_AGE_H

    fields = list(pa["data_fields"])
    if not meta_fresh:
        fields += pa["metadata_fields"]

    resp = requests.get(
        f"{pa['base_url']}/sensors",
        headers={"X-API-Key": key},
        params={
            "fields": ",".join(fields),
            "location_type": pa["location_type"],
            "max_age": pa["max_age_s"],
            "nwlng": bbox["nwlng"],
            "nwlat": bbox["nwlat"],
            "selng": bbox["selng"],
            "selat": bbox["selat"],
        },
        timeout=60,
    )
    resp.raise_for_status()
    points = resp.headers.get("X-API-Points-Consumed") or resp.headers.get(
        "x-api-points-consumed"
    )
    print(f"PurpleAir: HTTP {resp.status_code}, points consumed: {points}")
    body = resp.json()
    cols = body["fields"]
    idx = {c: i for i, c in enumerate(cols)}

    now = utcnow()
    rows = []
    for rec in body["data"]:
        sid = f"pa_{rec[idx['sensor_index']]}"
        if not meta_fresh:
            sensors_meta.setdefault("sensors", {})[sid] = {
                "name": rec[idx["name"]],
                "lat": rec[idx["latitude"]],
                "lon": rec[idx["longitude"]],
                "network": "purpleair",
            }
        meta = sensors_meta.get("sensors", {}).get(sid)
        if not meta:
            continue  # new sensor; appears after next metadata refresh
        rows.append(
            {
                "ts": iso(now),
                "id": sid,
                "network": "purpleair",
                "pm1_raw": rec[idx["pm1.0_atm"]],
                "pm25_cf1": rec[idx["pm2.5_cf_1"]],
                "pm25_atm": rec[idx["pm2.5_atm"]],
                "pm10_raw": rec[idx["pm10.0_atm"]],
                "rh": rec[idx["humidity"]],
            }
        )
        meta["last_seen"] = iso(now)
    if not meta_fresh:
        sensors_meta["purpleair_meta_fetched_at"] = iso(now)
    print(f"PurpleAir: {len(rows)} sensors reporting")
    return rows, sensors_meta


# Egg topics/payloads vary by firmware; match species tokens loosely.
EGG_SPECIES_PATTERNS = {
    "pm1": re.compile(r"pm[\-_.]?1(\.0|p0)?(?![0-9.])", re.I),
    "pm25": re.compile(r"pm[\-_.]?2[\._p]?5", re.I),
    "pm10": re.compile(r"pm[\-_.]?10(?![0-9.])", re.I),
}


def egg_value_from_message(msg):
    """Pull a numeric concentration from an Egg message payload."""
    payload = msg.get("payload", msg)
    for key in ("converted-value", "converted_value", "compensated-value", "value"):
        v = payload.get(key)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def fetch_eggs(cfg):
    aqe = cfg["airqualityegg"]
    serials = aqe.get("serials") or []
    if not serials:
        return [], {}
    key = os.environ.get("AQE_API_KEY")
    if not key:
        print("WARNING: AQE_API_KEY not set; skipping Air Quality Egg")
        return [], {}

    now = utcnow()
    rows, meta = [], {}
    for entry in serials:
        if isinstance(entry, dict):
            serial = entry["serial"]
            egg_meta = {
                "name": entry.get("name", serial),
                "lat": entry.get("lat"),
                "lon": entry.get("lon"),
                "network": "egg",
            }
        else:
            serial, egg_meta = entry, {"name": entry, "lat": None, "lon": None, "network": "egg"}
        sid = f"egg_{serial}"
        try:
            resp = requests.get(
                f"{aqe['base_url']}/most-recent/messages/device/{serial}",
                params={"apiKey": key},
                timeout=60,
            )
            resp.raise_for_status()
            body = resp.json()
        except Exception as e:
            print(f"Egg {serial}: fetch failed: {e}")
            continue

        messages = body if isinstance(body, list) else body.get("messages", [body])
        row = {"ts": iso(now), "id": sid, "network": "egg"}
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            topic = str(msg.get("topic", "")) + " " + str(
                msg.get("payload", {}).get("sensor-part-number", "")
            )
            payload = msg.get("payload", msg)
            # Prefer explicit lat/lon from the message
            for latk, lonk in (("latitude", "longitude"), ("lat", "lon")):
                if isinstance(payload.get(latk), (int, float)):
                    egg_meta["lat"] = payload[latk]
                    egg_meta["lon"] = payload[lonk]
            # pm10 pattern must be checked before pm1 ("pm10" contains "pm1")
            for sp in ("pm10", "pm25", "pm1"):
                if sp in row:
                    continue
                if EGG_SPECIES_PATTERNS[sp].search(topic):
                    v = egg_value_from_message(msg)
                    if v is not None:
                        row[sp] = v
        if any(sp in row for sp in SPECIES):
            rows.append(row)
            meta[sid] = egg_meta
        else:
            print(f"Egg {serial}: no PM values found in most-recent messages")
    print(f"Eggs: {len(rows)} of {len(serials)} reporting")
    return rows, meta


def build_frames(cfg, sensors_meta, calibration):
    """Rebuild frames_{window}.json from the archive."""
    max_hours = max(w["hours"] for w in cfg["windows"])
    rows = read_archive(max_age_days=max_hours / 24 + 1)
    now = utcnow()
    known = sensors_meta.get("sensors", {})

    for win in cfg["windows"]:
        step = win["step_minutes"]
        start = floor_to_slot(now - timedelta(hours=win["hours"]), step)
        end = floor_to_slot(now, step)
        n_slots = int((end - start).total_seconds() // (step * 60)) + 1
        times = [start + timedelta(minutes=step * i) for i in range(n_slots)]

        # slot index -> sensor id -> {species: [values]}
        acc = {}
        active_ids = set()
        for row in rows:
            ts = parse_iso(row["ts"])
            if ts < start or ts > end + timedelta(minutes=step):
                continue
            slot = int((floor_to_slot(ts, step) - start).total_seconds() // (step * 60))
            if not (0 <= slot < n_slots):
                continue
            sid = row["id"]
            if sid not in known:
                continue
            active_ids.add(sid)
            bucket = acc.setdefault(slot, {}).setdefault(sid, {})
            for sp, v in species_values(row, calibration).items():
                bucket.setdefault(sp, []).append(v)

        sensor_ids = sorted(active_ids)
        values = {sp: [] for sp in SPECIES}
        for slot in range(n_slots):
            per_sensor = acc.get(slot, {})
            for sp in SPECIES:
                frame = []
                for sid in sensor_ids:
                    vals = per_sensor.get(sid, {}).get(sp)
                    frame.append(round(sum(vals) / len(vals), 1) if vals else None)
                values[sp].append(frame)

        out = {
            "generated_at": iso(now),
            "window": win["key"],
            "step_minutes": step,
            "times": [iso(t) for t in times],
            "sensors": sensor_ids,
            "values": values,
        }
        path = DATA_DIR / f"frames_{win['key']}.json"
        write_json(path, out)
        print(f"frames_{win['key']}.json: {n_slots} frames x {len(sensor_ids)} sensors")


def make_synthetic_archive(cfg):
    """Generate 7 days of plausible fake data for frontend development."""
    random.seed(42)
    center_lat, center_lon = cfg["region"]["center"]
    sensors = {}
    for i in range(12):
        sensors[f"pa_{1000 + i}"] = {
            "name": f"Synthetic PA {i}",
            "lat": center_lat + random.uniform(-0.09, 0.11),
            "lon": center_lon + random.uniform(-0.15, 0.15),
            "network": "purpleair",
        }
    for i in range(3):
        sensors[f"egg_synth{i}"] = {
            "name": f"Synthetic Egg {i}",
            "lat": center_lat + random.uniform(-0.05, 0.05),
            "lon": center_lon + random.uniform(-0.08, 0.08),
            "network": "egg",
        }

    now = floor_to_slot(utcnow(), 15)
    rows = []
    for k in range(7 * 96, -1, -1):
        ts = now - timedelta(minutes=15 * k)
        hour = ts.hour + ts.minute / 60
        # morning/evening wood-smoke bumps + a mid-week "smoke event"
        diurnal = 4 + 6 * math.exp(-((hour - 7) ** 2) / 6) + 8 * math.exp(-((hour - 20) ** 2) / 8)
        event = 40 * math.exp(-((k - 350) ** 2) / 4000)
        for sid, m in sensors.items():
            local = diurnal + event + random.gauss(0, 1.5) + (m["lat"] - center_lat) * 30
            pm25 = max(local, 0.5)
            row = {
                "ts": iso(ts),
                "id": sid,
                "network": m["network"],
                "pm1": round(pm25 * 0.6, 1),
                "pm25": round(pm25, 1),
                "pm10": round(pm25 * 1.8, 1),
            }
            if random.random() < 0.02:
                continue  # occasional dropout
            rows.append(row)
        for m in sensors.values():
            m["last_seen"] = iso(ts)

    if ARCHIVE_PATH.exists():
        ARCHIVE_PATH.unlink()
    append_archive(rows, keep_days=cfg["archive"]["keep_days"])
    return {"sensors": sensors, "purpleair_meta_fetched_at": iso(now)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synthetic", action="store_true", help="generate fake data")
    ap.add_argument("--frames-only", action="store_true", help="rebuild frames only")
    args = ap.parse_args()

    cfg = load_config()
    DATA_DIR.mkdir(exist_ok=True)
    calibration = load_json(CALIBRATION_PATH) or default_calibration()
    write_json(CALIBRATION_PATH, calibration)
    sensors_meta = load_json(SENSORS_PATH, default={"sensors": {}})

    if args.synthetic:
        sensors_meta = make_synthetic_archive(cfg)
    elif not args.frames_only:
        pa_rows, sensors_meta = fetch_purpleair(cfg, sensors_meta)
        egg_rows, egg_meta = fetch_eggs(cfg)
        sensors_meta.setdefault("sensors", {}).update(egg_meta)
        all_rows = pa_rows + egg_rows
        if all_rows:
            append_archive(all_rows, keep_days=cfg["archive"]["keep_days"])
        elif ARCHIVE_PATH.exists():
            print("WARNING: no new data this run; rebuilding frames from archive")
        else:
            print("ERROR: no data fetched and no archive (are API keys set?)")
            sys.exit(1)

    write_json(SENSORS_PATH, sensors_meta)
    build_frames(cfg, sensors_meta, calibration)
    print("Done.")


if __name__ == "__main__":
    main()
