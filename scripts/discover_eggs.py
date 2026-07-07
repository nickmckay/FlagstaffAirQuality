"""Find Air Quality Egg devices near Flagstaff.

Queries the historical messages-by-topic endpoint with a lat/lon/radius
filter over the last few days and reports the serial numbers, locations,
and species seen. Paste the results into config.yaml under
airqualityegg.serials, e.g.:

  serials:
    - serial: egg00802aaa019b0111
      name: Downtown Egg
      lat: 35.198
      lon: -111.651

Requires AQE_API_KEY. Usage: python scripts/discover_eggs.py [--days 3]
"""

import argparse
import os
import re
import sys
from collections import defaultdict
from datetime import timedelta
from urllib.parse import quote

import requests

from common import iso, load_config, utcnow

# Egg MQTT topics look like /orgs/wd/aqe/<sensor-type>/<serial>; '#' and '+'
# are MQTT wildcards. Try broad to narrow until one returns data.
CANDIDATE_TOPICS = [
    "/orgs/wd/aqe/#",
    "/orgs/wd/aqe/+/+",
    "#",
]

SERIAL_RE = re.compile(r"egg[0-9a-f]{16,}", re.I)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()

    key = os.environ.get("AQE_API_KEY")
    if not key:
        sys.exit("Set AQE_API_KEY first (Egg web portal > Account Settings)")

    cfg = load_config()
    lat, lon = cfg["region"]["center"]
    radius = cfg["region"]["egg_search_radius_m"]
    base = cfg["airqualityegg"]["base_url"]
    params = {
        "apiKey": key,
        "lat": lat,
        "lon": lon,
        "radius": radius,
        "start-date": iso(utcnow() - timedelta(days=args.days)),
        "end-date": iso(utcnow()),
        "resolution": "PT1H",
    }

    messages = None
    for topic in CANDIDATE_TOPICS:
        url = f"{base}/messages/topic/{quote(topic, safe='')}"
        print(f"Trying topic {topic!r} ...")
        try:
            resp = requests.get(url, params=params, timeout=120)
            print(f"  HTTP {resp.status_code}")
            if resp.status_code != 200:
                continue
            body = resp.json()
            msgs = body if isinstance(body, list) else body.get("messages", [])
            if msgs:
                messages = msgs
                break
            print("  200 but no messages")
        except Exception as e:
            print(f"  failed: {e}")
    if not messages:
        sys.exit(
            "No messages found. The topic scheme may differ; inspect one of "
            "your own eggs with the most-recent endpoint to see its topics."
        )

    eggs = defaultdict(lambda: {"topics": set(), "lat": None, "lon": None, "n": 0})
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        topic = str(msg.get("topic", ""))
        payload = msg.get("payload", msg) if isinstance(msg.get("payload"), dict) else msg
        m = SERIAL_RE.search(topic) or SERIAL_RE.search(str(payload.get("serial-number", "")))
        if not m:
            continue
        serial = m.group(0).lower()
        info = eggs[serial]
        info["topics"].add(topic)
        info["n"] += 1
        for latk, lonk in (("latitude", "longitude"), ("lat", "lon")):
            if isinstance(payload.get(latk), (int, float)):
                info["lat"], info["lon"] = payload[latk], payload[lonk]

    if not eggs:
        sys.exit("Messages returned but no egg serials recognized; inspect raw output.")

    print(f"\nFound {len(eggs)} egg(s) within {radius/1000:.0f} km:\n")
    print("serials:")
    for serial, info in sorted(eggs.items()):
        print(f"  - serial: {serial}")
        if info["lat"] is not None:
            print(f"    lat: {info['lat']}")
            print(f"    lon: {info['lon']}")
        print(f"    # {info['n']} messages, topics: {sorted(info['topics'])[:3]}")


if __name__ == "__main__":
    main()
