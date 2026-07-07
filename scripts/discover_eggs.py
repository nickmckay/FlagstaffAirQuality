"""Find Air Quality Egg devices near Flagstaff.

Uses the public /eggs/mapped endpoint (no auth), which lists every mapped egg
with its serial, coordinates, and latest readings. Prints YAML ready to paste
into config.yaml under airqualityegg.serials.

Usage: python scripts/discover_eggs.py [--radius-km 40]
"""

import argparse

import requests

from common import haversine_m, load_config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--radius-km", type=float, default=None)
    args = ap.parse_args()

    cfg = load_config()
    clat, clon = cfg["region"]["center"]
    radius_m = (
        args.radius_km * 1000
        if args.radius_km
        else cfg["region"]["egg_search_radius_m"]
    )
    base = cfg["airqualityegg"]["base_url"]

    eggs = requests.get(f"{base}/eggs/mapped", timeout=120).json()
    print(f"{len(eggs)} mapped eggs worldwide; filtering to {radius_m/1000:.0f} km")

    found = []
    for egg in eggs:
        coords = (egg.get("loc") or {}).get("coordinates")
        if not coords or len(coords) < 2:
            continue
        lon, lat = coords[0], coords[1]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        if haversine_m(clat, clon, lat, lon) > radius_m:
            continue
        last = {i.get("label"): i for i in egg.get("lastData", []) if isinstance(i, dict)}
        pm_ts = next(
            (last[k].get("timestamp") for k in ("pm2p5", "pm1p0", "pm10p0") if k in last),
            None,
        )
        found.append((egg["serial_number"], lat, lon, pm_ts))

    if not found:
        print("No eggs found in radius.")
        return
    print(f"\n{len(found)} egg(s) found. Paste into config.yaml:\n")
    print("  serials:")
    for serial, lat, lon, pm_ts in sorted(found):
        print(f"    - serial: {serial}")
        print(f"      name: {serial}")
        print(f"      lat: {round(lat, 4)}")
        print(f"      lon: {round(lon, 4)}")
        print(f"      # last PM reading: {pm_ts or 'never'}")


if __name__ == "__main__":
    main()
