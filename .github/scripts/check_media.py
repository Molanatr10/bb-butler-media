"""Probe the public media route before requesting a replacement deployment."""

import json
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


HEALTH_URL = "https://bb-butler-media.hostless.app/health"


def healthy():
    request = Request(HEALTH_URL, headers={"User-Agent": "Nokar-health-check"})
    try:
        with urlopen(request, timeout=12) as response:
            data = json.loads(response.read(4096))
            return response.status == 200 and isinstance(data, dict) and data.get("ok") is True
    except (URLError, TimeoutError, OSError, ValueError):
        return False


def main():
    for attempt in range(3):
        if healthy():
            print("Media route is healthy.")
            return 0
        if attempt < 2:
            time.sleep(10)
    print("Media route failed three consecutive checks.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
