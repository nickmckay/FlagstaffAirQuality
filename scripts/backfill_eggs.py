"""One-off backfill of Air Quality Egg history into the archive.

Pulls full_particulate history (native 1-minute cadence, bucketed to 10-min
means) for every egg in config.yaml via the authorized by-topic endpoint,
chunked by day. Free API; no point budget.

Usage:
  python scripts/backfill_eggs.py [--days 10]

Run fetch_data.py --frames-only afterwards to rebuild frames.
"""

import argparse
import os
import sys
import time as time_mod
from datetime import timedelta

from common import append_archive, fetch_egg_history, load_config, utcnow
from fetch_data import egg_config_entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    key = os.environ.get("AQE_API_KEY")
    if not key:
        sys.exit("Set AQE_API_KEY first")

    cfg = load_config()
    base_url = cfg["airqualityegg"]["base_url"]
    entries = egg_config_entries(cfg)
    if not entries:
        sys.exit("No eggs in config.yaml")

    now = utcnow()
    all_rows = []
    for serial, name, _, _ in entries:
        got = 0
        for day in range(args.days, 0, -1):
            start = now - timedelta(days=day)
            end = now - timedelta(days=day - 1)
            try:
                rows = fetch_egg_history(key, base_url, serial, start, end)
            except Exception as e:
                print(f"{serial} day -{day}: {e}")
                continue
            all_rows.extend(rows)
            got += len(rows)
            time_mod.sleep(0.5)
        print(f"{name or serial}: {got} bucketed rows over {args.days} days")

    print(f"Backfilled {len(all_rows)} rows total")
    append_archive(all_rows, keep_days=cfg["archive"]["keep_days"])
    print("Archive updated. Now run: python scripts/fetch_data.py --frames-only")


if __name__ == "__main__":
    main()
