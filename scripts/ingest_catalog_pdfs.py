#!/usr/bin/env python3
"""Upload external campus catalog PDFs and addendums to the
per-campus Gemini File Search stores.

Layout:
  data/source_documents/catalogs/{CAMPUS}-Catalog-2025-2026.pdf
  data/source_documents/catalogs/addendums/{CAMPUS}-*.pdf

Usage:
  PYTHONPATH=/app:/app/backend python scripts/ingest_catalog_pdfs.py
  PYTHONPATH=/app:/app/backend python scripts/ingest_catalog_pdfs.py --campus LAMC ELAC
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

DEFAULT_SOURCE_DIR = PROJECT_DIR / "data" / "source_documents" / "catalogs"

# Map campus → primary catalog filename (LAPC has a non-standard name)
PRIMARY_CATALOG = {
    "LAMC": "LAMC-Catalog-2025-2026.pdf",
    "ELAC": "ELAC-Catalog-2025-2026.pdf",
    "LACC": "LACC-Catalog-2025-2026.pdf",
    "LAHC": "LAHC-Catalog-2025-2026.pdf",
    "LAPC": "Los Angeles Pierce College 2025-2026 General Catalog.pdf",
    "LASC": "LASC-Catalog-2025-2026.pdf",
    "LATTC": "LATTC-Catalog-2025-2026.pdf",
    "LAVC": "LAVC-Catalog-2025-2026.pdf",
    "WLAC": "WLAC-Catalog-2025-2026.pdf",
}


def get_or_create_store(client, display_name: str) -> str:
    for store in client.file_search_stores.list():
        if getattr(store, "display_name", None) == display_name:
            return store.name
    store = client.file_search_stores.create(config={"display_name": display_name})
    return store.name


def upload_pdf(client, store_name: str, pdf_path: Path, display_name: str) -> bool:
    existing = set()
    for doc in client.file_search_stores.documents.list(parent=store_name):
        n = getattr(doc, "display_name", None)
        if n:
            existing.add(n)
    if display_name in existing:
        return False
    op = client.file_search_stores.upload_to_file_search_store(
        file=str(pdf_path),
        file_search_store_name=store_name,
        config={"display_name": display_name, "mime_type": "application/pdf"},
    )
    # Wait for the long-running op to finish
    while not op.done:
        time.sleep(2)
        op = client.operations.get(op)
    return True


def find_addendums_for(code: str, source_dir: Path) -> list[Path]:
    addendum_dir = source_dir / "addendums"
    if not addendum_dir.exists():
        return []
    return sorted(p for p in addendum_dir.glob(f"{code}-*.pdf") if p.is_file())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Upload catalog PDFs to per-campus Gemini File Search stores")
    parser.add_argument("--campus", nargs="+", default=None, help="Campus codes (default: all 9)")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"External catalog directory (default: {DEFAULT_SOURCE_DIR})",
    )
    return parser


def main():
    args = build_parser().parse_args()

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
        primary = args.source_dir / PRIMARY_CATALOG[code]
        if not primary.exists():
            print(f"  SKIP {code}: catalog not found at {primary}", flush=True)
            continue

        store_display_name = f"{code.lower()}-chatbot-file-search"
        store_name = get_or_create_store(client, store_display_name)

        files_to_upload = [primary] + find_addendums_for(code, args.source_dir)
        uploaded = 0
        skipped = 0
        for pdf in files_to_upload:
            display_name = f"catalog-{pdf.name}"
            try:
                if upload_pdf(client, store_name, pdf, display_name):
                    uploaded += 1
                    print(f"  [{code}] uploaded {pdf.name} ({pdf.stat().st_size // 1024} KB)", flush=True)
                else:
                    skipped += 1
                    print(f"  [{code}] already present: {pdf.name}", flush=True)
            except Exception as e:
                print(f"  [{code}] ERROR {pdf.name}: {e}", flush=True)
        summary[code] = {"uploaded": uploaded, "skipped": skipped, "store": store_name}
        print(f"  {code} done: uploaded={uploaded} skipped={skipped}", flush=True)

    print("\n=== Summary ===")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
