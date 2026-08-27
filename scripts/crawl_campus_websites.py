#!/usr/bin/env python3
"""Crawl all 9 LACCD campus websites using Firecrawl and save markdown output.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/crawl_campus_websites.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/crawl_campus_websites.py --campus ELAC LACC
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/crawl_campus_websites.py --limit 50
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

try:
    from firecrawl import FirecrawlApp
except ImportError:
    print("ERROR: firecrawl-py not installed. Run: pip install firecrawl-py")
    raise SystemExit(1)

from shared.campus_config import CAMPUSES
from shared.content_filters import should_purge

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "campus_crawls"
MAX_REMOTE_PAGE_BYTES = 2 * 1024 * 1024
MAX_CAMPUS_BYTES = 256 * 1024 * 1024

# Regex to filter out pre-2020 content
_PRE_2020_RE = re.compile(r"\b(19\d{2}|200\d|201\d)\b")
_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def is_pre_2020(text: str) -> bool:
    """Check if the dominant year references in text are pre-2020."""
    years = [int(y) for y in _YEAR_RE.findall(text)]
    if not years:
        return False
    # If most years are pre-2020, skip
    old = sum(1 for y in years if y < 2020)
    return old > len(years) / 2


def safe_filename(url: str) -> str:
    """Convert a URL to a safe filename."""
    # Strip protocol and domain
    path = re.sub(r"https?://[^/]+", "", url)
    # Convert slashes and special chars to underscores
    name = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_")
    if not name:
        name = "index"
    # Truncate and add hash to avoid collisions
    if len(name) > 80:
        h = hashlib.md5(url.encode()).hexdigest()[:8]
        name = name[:72] + "_" + h
    return name + ".txt"


def crawl_campus(campus_code: str, limit: int = 150, *, filter_noise: bool = True) -> list[Path]:
    """Crawl a single campus website and save markdown files."""
    campus = CAMPUSES[campus_code]
    api_key = os.getenv("FIRECRAWL_API_KEY")
    if not api_key:
        print(f"  ERROR: FIRECRAWL_API_KEY not set, skipping {campus_code}")
        return []

    app = FirecrawlApp(api_key=api_key)
    output_dir = DATA_DIR / campus_code
    output_dir.mkdir(parents=True, exist_ok=True)

    url = f"https://{campus.website}"
    print(f"  Crawling {url} (limit={limit})...")

    try:
        result = app.crawl(
            url,
            limit=limit,
            poll_interval=5,
            max_concurrency=2,
            delay=2,
        )
    except Exception as e:
        print(f"  ERROR crawling {campus_code}: {e}")
        return []

    saved = []
    skipped = 0
    saved_bytes = sum(
        path.stat().st_size for path in output_dir.glob("*.txt") if path.is_file()
    )
    if saved_bytes > MAX_CAMPUS_BYTES:
        raise RuntimeError(
            f"{campus_code}: retained campus crawl already exceeds {MAX_CAMPUS_BYTES} bytes"
        )
    data = getattr(result, "data", []) if hasattr(result, "data") else result.get("data", []) if isinstance(result, dict) else []
    if not data:
        print(f"  WARNING: No pages returned for {campus_code}")
        return []

    for page in data:
        # Firecrawl v4 returns Pydantic models; handle both dict and object access
        md = getattr(page, "markdown", "") or (page.get("markdown", "") if isinstance(page, dict) else "")
        metadata = getattr(page, "metadata", None)
        if metadata and not isinstance(metadata, dict):
            page_url = getattr(metadata, "source_url", "") or getattr(metadata, "sourceURL", "") or ""
        elif isinstance(metadata, dict):
            page_url = metadata.get("sourceURL", "") or metadata.get("source_url", "")
        else:
            page_url = getattr(page, "url", "") or ""

        if not md or len(md.strip()) < 100:
            continue
        encoded_size = len(md.encode("utf-8"))
        if encoded_size > MAX_REMOTE_PAGE_BYTES:
            raise RuntimeError(f"{campus_code}: page exceeds {MAX_REMOTE_PAGE_BYTES} bytes")
        if saved_bytes + encoded_size > MAX_CAMPUS_BYTES:
            raise RuntimeError(f"{campus_code}: crawl exceeds {MAX_CAMPUS_BYTES} bytes")
        if is_pre_2020(md):
            continue

        filename = safe_filename(page_url or f"page_{len(saved)}")

        # Filter out noise pages before writing to disk
        if filter_noise and should_purge(filename):
            skipped += 1
            continue

        filepath = output_dir / filename
        filepath.write_text(md, encoding="utf-8")
        saved.append(filepath)
        saved_bytes += encoded_size

    print(f"  Saved {len(saved)} pages for {campus_code} to {output_dir}")
    if skipped:
        print(f"  Filtered out {skipped} noise pages")
    return saved


def main():
    parser = argparse.ArgumentParser(description="Crawl LACCD campus websites using Firecrawl")
    parser.add_argument(
        "--campus",
        nargs="+",
        default=None,
        help="Campus codes to crawl (e.g. ELAC LACC). Default: all non-LAMC campuses.",
    )
    parser.add_argument("--limit", type=int, default=150, help="Max pages per campus (default: 150)")
    parser.add_argument("--no-filter", action="store_true", help="Disable noise filtering (save all pages for diagnostics)")
    parser.add_argument("--include-lamc", action="store_true", help="Also crawl LAMC (already done separately)")
    args = parser.parse_args()

    if args.campus:
        codes = [c.upper() for c in args.campus]
    else:
        codes = [c for c in CAMPUSES if c != "LAMC" or args.include_lamc]

    print(f"Crawling {len(codes)} campus(es): {', '.join(codes)}")
    summary = {}

    for code in codes:
        if code not in CAMPUSES:
            print(f"  SKIP: Unknown campus code '{code}'")
            continue
        print(f"\n--- {code}: {CAMPUSES[code].name} ---")
        saved = crawl_campus(code, limit=args.limit, filter_noise=not args.no_filter)
        summary[code] = len(saved)
        if code != codes[-1]:
            time.sleep(2)  # Be polite between campuses

    print("\n=== Summary ===")
    for code, count in summary.items():
        print(f"  {code}: {count} pages")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
