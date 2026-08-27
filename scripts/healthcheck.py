#!/usr/bin/env python3
"""Uptime/health probe for the LACCD chatbot API.

Hits ``GET /api/health`` and exits non-zero when the service is degraded or
unreachable, so it can drive a cron job or an external uptime monitor
(Uptime Kuma, healthchecks.io, a Caddy/systemd timer, etc.). Stdlib-only — no
project imports, no PYTHONPATH needed, so it runs from anywhere.

Exit codes:
  0  healthy      (HTTP 200 and body {"status": "healthy"})
  1  degraded     (reachable but status != "healthy")
  2  unreachable  (connection error, timeout, non-200, or unparseable body)

Usage:
  python scripts/healthcheck.py                         # http://127.0.0.1:8000
  python scripts/healthcheck.py --url https://lamc-chat.johnnyrobot.ai
  HEALTHCHECK_URL=https://lamc-chat.johnnyrobot.ai python scripts/healthcheck.py
  python scripts/healthcheck.py --host-header lamc-chat.johnnyrobot.ai  # probe behind proxy

Cron example (alert on any non-zero exit):
  */5 * * * * /usr/bin/python3 /opt/laccd/scripts/healthcheck.py \
      --url http://127.0.0.1:8000 || echo "laccd-chatbot health FAILED" | mail -s alert ops@example.com
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

EXIT_HEALTHY = 0
EXIT_DEGRADED = 1
EXIT_UNREACHABLE = 2


def probe(base_url: str, *, timeout: float, host_header: str | None) -> int:
    url = base_url.rstrip("/") + "/api/health"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    if host_header:
        req.add_header("Host", host_header)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status_code = resp.getcode()
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        print(f"UNREACHABLE {url} HTTP {exc.code}", file=sys.stderr)
        return EXIT_UNREACHABLE
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"UNREACHABLE {url} ({exc})", file=sys.stderr)
        return EXIT_UNREACHABLE

    if status_code != 200:
        print(f"UNREACHABLE {url} HTTP {status_code}", file=sys.stderr)
        return EXIT_UNREACHABLE

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        print(f"UNREACHABLE {url} (invalid JSON body)", file=sys.stderr)
        return EXIT_UNREACHABLE

    if not isinstance(body, dict):
        print(f"UNREACHABLE {url} (unexpected JSON body)", file=sys.stderr)
        return EXIT_UNREACHABLE

    status = body.get("status")
    services = body.get("services", {})
    if status == "healthy":
        print(f"HEALTHY {url} services={services}")
        return EXIT_HEALTHY
    print(f"DEGRADED {url} status={status} services={services}", file=sys.stderr)
    return EXIT_DEGRADED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe /api/health and exit non-zero if not healthy.")
    parser.add_argument(
        "--url",
        default=os.getenv("HEALTHCHECK_URL", "http://127.0.0.1:8000"),
        help="Base URL of the API (default: $HEALTHCHECK_URL or http://127.0.0.1:8000).",
    )
    parser.add_argument(
        "--host-header",
        default=os.getenv("HEALTHCHECK_HOST_HEADER"),
        help="Optional Host header to send (probe a specific campus vhost behind the proxy).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("HEALTHCHECK_TIMEOUT", "10")),
        help="Request timeout in seconds (default: 10).",
    )
    args = parser.parse_args(argv)
    return probe(args.url, timeout=args.timeout, host_header=args.host_header)


if __name__ == "__main__":
    raise SystemExit(main())
