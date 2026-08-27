#!/usr/bin/env python3
"""Upload Docling-converted catalog markdown files to per-campus Gemini File Search stores.

Reads from data/catalog_md/{CAMPUS}/*.md (catalog.md + addendum-*.md) and
uploads each as a `catalog-md-{filename}` document. Dedupes by display_name,
so safe to re-run.

Usage:
  PYTHONPATH=/app:/app/backend python scripts/ingest_catalog_md.py
  PYTHONPATH=/app:/app/backend python scripts/ingest_catalog_md.py --campus LASC
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from google import genai

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "backend"))

from shared.campus_config import CAMPUSES  # noqa: E402

MD_DIR = PROJECT_DIR / "data" / "catalog_md"


def get_or_create_store(client, display_name: str) -> str:
    for store in client.file_search_stores.list():
        if getattr(store, "display_name", None) == display_name:
            return store.name
    store = client.file_search_stores.create(config={"display_name": display_name})
    return store.name


def upload_md(client, store_name: str, md_path: Path, display_name: str) -> bool:
    existing = set()
    for doc in client.file_search_stores.documents.list(parent=store_name):
        n = getattr(doc, "display_name", None)
        if n:
            existing.add(n)
    if display_name in existing:
        return False
    op = client.file_search_stores.upload_to_file_search_store(
        file=str(md_path),
        file_search_store_name=store_name,
        config={"display_name": display_name, "mime_type": "text/markdown"},
    )
    while not op.done:
        time.sleep(2)
        op = client.operations.get(op)
    return True


def main():
    parser = argparse.ArgumentParser(description="Upload catalog markdown to per-campus File Search stores")
    parser.add_argument("--campus", nargs="+", default=None, help="Campus codes (default: all 9)")
    args = parser.parse_args()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY not set", flush=True)
        raise SystemExit(1)
    client = genai.Client(api_key=api_key)

    codes = [c.upper() for c in args.campus] if args.campus else list(CAMPUSES.keys())
    summary = {}

    for code in codes:
        if code not in CAMPUSES:
            print(f"unknown campus: {code}", flush=True)
            continue
        campus_dir = MD_DIR / code
        if not campus_dir.exists():
            print(f"  SKIP {code}: no markdown at {campus_dir}", flush=True)
            continue
        md_files = sorted(campus_dir.glob("*.md"))
        if not md_files:
            print(f"  SKIP {code}: no .md files in {campus_dir}", flush=True)
            continue

        store_display_name = f"{code.lower()}-chatbot-file-search"
        store_name = get_or_create_store(client, store_display_name)

        uploaded = 0
        skipped = 0
        for md in md_files:
            display_name = f"catalog-md-{code}-{md.name}"
            try:
                if upload_md(client, store_name, md, display_name):
                    uploaded += 1
                    print(f"  [{code}] uploaded {md.name} ({md.stat().st_size // 1024} KB)", flush=True)
                else:
                    skipped += 1
                    print(f"  [{code}] already present: {md.name}", flush=True)
            except Exception as e:
                print(f"  [{code}] ERROR {md.name}: {e}", flush=True)
        summary[code] = {"uploaded": uploaded, "skipped": skipped, "store": store_name}

    print("\n=== Summary ===")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
