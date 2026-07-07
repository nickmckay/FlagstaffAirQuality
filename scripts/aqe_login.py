"""Log in to the Air Quality Egg API and store the JWT in .env as AQE_JWT.

Prompts for the password (not echoed, not stored). Run interactively:
  python3 scripts/aqe_login.py [email]
"""

import getpass
import json
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"


def main():
    # the API expects {"name": <portal username or email>, "password": ...}
    name = sys.argv[1] if len(sys.argv) > 1 else input("Username: ")
    password = getpass.getpass("Air Quality Egg password (not stored): ")
    resp = requests.post(
        "https://airqualityegg.com/api/v2/login",
        json={"name": name, "password": password},
        timeout=60,
    )
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(resp.text[:300])
        sys.exit(1)
    body = resp.json()
    token = body.get("jwt") or body.get("token") or body.get("access_token")
    if not token:
        print("No token field found; response keys:", list(body))
        print(json.dumps(body)[:500])
        sys.exit(1)

    lines = []
    if ENV_PATH.exists():
        lines = [
            l for l in ENV_PATH.read_text().splitlines() if not l.startswith("AQE_JWT=")
        ]
    lines.append(f"AQE_JWT={token}")
    ENV_PATH.write_text("\n".join(lines) + "\n")
    print(f"JWT saved to {ENV_PATH} (AQE_JWT). Other response keys: {list(body)}")


if __name__ == "__main__":
    main()
