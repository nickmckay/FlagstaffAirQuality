"""One-off backfill of PurpleAir history into the archive.

Pulls /v1/sensors/{index}/history for every PurpleAir sensor already in
data/sensors.json: 10-minute averages over the recent fine window (feeds the
15-minute animation frames) and hourly averages over the full span (feeds the
7-day window and calibration pairs). Costs API points: roughly
2 + 6 x rows per call (~100k points for 22 sensors, 48h fine + 7d hourly).

Usage:
  python scripts/backfill_purpleair.py [--days 7] [--fine-hours 48]

Run fetch_data.py --frames-only afterwards to rebuild frames.
"""

import argparse
import os
import sys
import time as time_mod
from datetime import datetime, timedelta, timezone

import requests

from common import (
    SENSORS_PATH,
    append_archive,
    iso,
    load_config,
    load_json,
    utcnow,
)

HISTORY_FIELDS = ["pm1.0_atm", "pm2.5_cf_1", "pm2.5_atm", "pm10.0_atm", "humidity"]
FIELD_TO_ROWKEY = {
    "pm1.0_atm": "pm1_raw",
    "pm2.5_cf_1": "pm25_cf1",
    "pm2.5_atm": "pm25_atm",
    "pm10.0_atm": "pm10_raw",
    "humidity": "rh",
}


def fetch_history(key, base_url, sensor_index, start, end, average_min):
    resp = requests.get(
        f"{base_url}/sensors/{sensor_index}/history",
        headers={"X-API-Key": key},
        params={
            "fields": ",".join(HISTORY_FIELDS),
            "start_timestamp": int(start.timestamp()),
            "end_timestamp": int(end.timestamp()),
            "average": average_min,
        },
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    idx = {c: i for i, c in enumerate(body["fields"])}
    rows = []
    for rec in body["data"]:
        ts = rec[idx["time_stamp"]]
        row = {
            "ts": iso(datetime.fromtimestamp(ts, tz=timezone.utc)),
            "id": f"pa_{sensor_index}",
            "network": "purpleair",
        }
        for field, rowkey in FIELD_TO_ROWKEY.items():
            v = rec[idx.get(field, -1)] if field in idx else None
            if isinstance(v, (int, float)):
                row[rowkey] = v
        rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--fine-hours", type=int, default=48)
    args = ap.parse_args()

    key = os.environ.get("PURPLEAIR_API_KEY")
    if not key:
        sys.exit("Set PURPLEAIR_API_KEY first")

    cfg = load_config()
    base_url = cfg["purpleair"]["base_url"]
    sensors = (load_json(SENSORS_PATH, default={}) or {}).get("sensors", {})
    pa_ids = sorted(s for s, m in sensors.items() if m["network"] == "purpleair")
    if not pa_ids:
        sys.exit("No PurpleAir sensors in data/sensors.json; run fetch_data.py first")

    now = utcnow()
    fine_start = now - timedelta(hours=args.fine_hours)
    span_start = now - timedelta(days=args.days)

    all_rows = []
    for i, sid in enumerate(pa_ids):
        index = sid.removeprefix("pa_")
        try:
            fine = fetch_history(key, base_url, index, fine_start, now, 10)
            hourly = fetch_history(key, base_url, index, span_start, fine_start, 60)
        except requests.HTTPError as e:
            print(f"{sid}: history failed: {e}")
            continue
        all_rows.extend(fine)
        all_rows.extend(hourly)
        print(f"[{i+1}/{len(pa_ids)}] {sid}: {len(fine)} fine + {len(hourly)} hourly rows")
        time_mod.sleep(1)  # be gentle with the API

    print(f"Backfilled {len(all_rows)} rows total")
    append_archive(all_rows, keep_days=cfg["archive"]["keep_days"])
    print("Archive updated. Now run: python scripts/fetch_data.py --frames-only")


if __name__ == "__main__":
    main()
