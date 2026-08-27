#!/usr/bin/env python3
"""Upload crawled campus content to per-campus Gemini File Search stores.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/ingest_campus_corpus.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/ingest_campus_corpus.py --campus ELAC LACC
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=False)
load_dotenv(".env.local", override=True)

try:
    from google import genai
except ImportError:
    print("ERROR: google-genai not installed. Run: pip install google-genai", flush=True)
    raise SystemExit(1)

from shared.campus_config import CAMPUSES

#: Minimum fraction of attempted uploads that must succeed before the run is
#: treated as a failure. Per-file errors are only warnings, so without this a run
#: where every upload failed still exits 0.
DEFAULT_MIN_YIELD = 0.5

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "campus_crawls"


def get_or_create_store(client, display_name: str) -> str:
    """Find or create a Gemini File Search store by display name."""
    # Check env var override first
    env_key = f"GEMINI_FILE_SEARCH_STORE_{display_name.split('-')[0].upper()}"
    env_val = os.getenv(env_key)
    if env_val:
        print(f"  Using store from env {env_key}: {env_val}", flush=True)
        return env_val

    # Search existing stores
    for store in client.file_search_stores.list():
        if getattr(store, "display_name", None) == display_name:
            print(f"  Found existing store: {store.name}", flush=True)
            return store.name

    # Create new store
    store = client.file_search_stores.create(config={"display_name": display_name})
    print(f"  Created new store: {store.name} ({display_name})", flush=True)
    return store.name


def upload_one(client, store_name: str, filepath: Path) -> bool:
    """Upload a single file. Returns True on success."""
    try:
        operation = client.file_search_stores.upload_to_file_search_store(
            file=str(filepath),
            file_search_store_name=store_name,
            config={
                "display_name": filepath.name,
                "chunking_config": {
                    "white_space_config": {
                        "max_tokens_per_chunk": 350,
                        "max_overlap_tokens": 50,
                    }
                },
            },
        )
        while not operation.done:
            time.sleep(2)
            operation = client.operations.get(operation=operation)
        return True
    except Exception as e:
        print(f"    WARN: Failed to upload {filepath.name}: {e}", flush=True)
        return False


def upload_to_store(
    client, store_name: str, files: list[Path], max_workers: int = 10
) -> tuple[int, int]:
    """Upload text files to a Gemini File Search store with parallelism.

    Returns ``(attempted, uploaded)``. ``attempted`` counts only files not
    already present in the store, so a caller can judge the success rate without
    an idempotent re-run (attempted == 0) looking like a total failure.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Get existing documents to avoid duplicates
    existing = set()
    for doc in client.file_search_stores.documents.list(parent=store_name):
        name = getattr(doc, "display_name", None)
        if name:
            existing.add(name)

    to_upload = [f for f in files if f.name not in existing]
    if not to_upload:
        return 0, 0

    print(f"  Uploading {len(to_upload)} new files ({len(existing)} already exist)...", flush=True)
    uploaded = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(upload_one, client, store_name, f): f for f in to_upload}
        for i, future in enumerate(as_completed(futures), 1):
            if future.result():
                uploaded += 1
            if i % 50 == 0:
                print(f"    ... {i}/{len(to_upload)} processed, {uploaded} uploaded", flush=True)

    return len(to_upload), uploaded


def main():
    parser = argparse.ArgumentParser(description="Ingest crawled campus content to Gemini File Search")
    parser.add_argument(
        "--campus",
        nargs="+",
        default=None,
        help="Campus codes to ingest (default: all with crawled data)",
    )
    parser.add_argument(
        "--min-yield",
        type=float,
        default=DEFAULT_MIN_YIELD,
        help=(
            "Fail (exit 1) if a campus successfully uploads less than this fraction "
            f"of the files it attempted. Default {DEFAULT_MIN_YIELD}; pass 0 to disable."
        ),
    )
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY or GOOGLE_API_KEY not set", flush=True)
        raise SystemExit(1)

    client = genai.Client(api_key=api_key)

    if args.campus:
        codes = [c.upper() for c in args.campus]
    else:
        # Auto-discover campuses with crawled data
        codes = []
        if DATA_DIR.exists():
            for d in sorted(DATA_DIR.iterdir()):
                if d.is_dir() and d.name.upper() in CAMPUSES:
                    codes.append(d.name.upper())

    if not codes:
        print("No campus data found. Run crawl_campus_websites.py first.", flush=True)
        return

    print(f"Ingesting {len(codes)} campus(es): {', '.join(codes)}", flush=True)
    summary = {}

    for code in codes:
        campus_dir = DATA_DIR / code
        if not campus_dir.exists():
            print(f"  SKIP: No crawl data for {code} at {campus_dir}", flush=True)
            continue

        # Skip dotfiles: the crawler's own ".crawled.txt" resume manifest matches
        # "*.txt" and would otherwise be ingested as a document — a bare list of
        # URLs, useless as retrieval content. macOS "._*" AppleDouble stubs are
        # excluded for the same reason.
        files = sorted(
            f for f in campus_dir.glob("*.txt") if not f.name.startswith(".")
        )
        if not files:
            print(f"  SKIP: No .txt files for {code}", flush=True)
            continue

        print(f"\n--- {code}: {CAMPUSES[code].name} ({len(files)} files) ---", flush=True)
        store_display_name = f"{code.lower()}-chatbot-file-search"
        store_name = get_or_create_store(client, store_display_name)
        attempted, uploaded = upload_to_store(client, store_name, files)
        summary[code] = {
            "files": len(files),
            "attempted": attempted,
            "uploaded": uploaded,
            "store": store_name,
        }
        print(f"  Uploaded {uploaded} new documents (of {len(files)} total)", flush=True)

    print("\n=== Summary ===", flush=True)
    print(json.dumps(summary, indent=2, default=str), flush=True)

    # Per-file upload failures are caught and logged as warnings, so a run where
    # most or all uploads fail still exits 0. Judge successes against attempted
    # (post-dedupe) so an idempotent re-run — attempted 0 — is not a failure.
    failures = []
    for code, s in summary.items():
        attempted, uploaded = s["attempted"], s["uploaded"]
        if not attempted:
            continue
        ratio = uploaded / attempted
        if args.min_yield > 0 and ratio < args.min_yield:
            failures.append(
                f"{code}: uploaded {uploaded}/{attempted} = {ratio:.0%} "
                f"(below {args.min_yield:.0%})"
            )

    if failures:
        print("\nFAILED upload-yield check:", flush=True)
        for f in failures:
            print(f"  {f}", flush=True)
        print(
            "\nCheck the API key, quota, and store names above. "
            "Re-run with --min-yield 0 to override.",
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
