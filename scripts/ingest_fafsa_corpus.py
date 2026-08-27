#!/usr/bin/env python3
"""Upload FAFSA content to all 9 campus Gemini File Search stores.

Uploads:
  - Crawled web pages from data/fafsa_content/web/*.txt
  - Extracted video transcripts from data/fafsa_content/transcripts/*.txt
  - PDF toolkits from data/source_documents/fafsa/pdf/*.pdf

All files are prefixed with 'fafsa--' in the store for easy identification.

Usage:
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/ingest_fafsa_corpus.py
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/ingest_fafsa_corpus.py --campus LAMC ELAC
"""
from __future__ import annotations

import argparse
import json
import os
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "data" / "source_documents" / "fafsa"


def get_or_create_store(client, display_name: str) -> str:
    """Find or create a Gemini File Search store by display name."""
    env_key = f"GEMINI_FILE_SEARCH_STORE_{display_name.split('-')[0].upper()}"
    env_val = os.getenv(env_key)
    if env_val:
        print(f"  Using store from env {env_key}: {env_val}", flush=True)
        return env_val

    for store in client.file_search_stores.list():
        if getattr(store, "display_name", None) == display_name:
            print(f"  Found existing store: {store.name}", flush=True)
            return store.name

    store = client.file_search_stores.create(config={"display_name": display_name})
    print(f"  Created new store: {store.name} ({display_name})", flush=True)
    return store.name


def upload_one(client, store_name: str, filepath: Path, display_name: str) -> bool:
    """Upload a single file. Returns True on success."""
    try:
        operation = client.file_search_stores.upload_to_file_search_store(
            file=str(filepath),
            file_search_store_name=store_name,
            config={
                "display_name": display_name,
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
        print(f"    WARN: Failed to upload {display_name}: {e}", flush=True)
        return False


def collect_files(
    *,
    web_dir: Path,
    transcript_dir: Path,
    pdf_dir: Path,
) -> list[tuple[Path, str]]:
    """Collect all FAFSA files with their display names.

    Returns list of (filepath, display_name) tuples.
    """
    files: list[tuple[Path, str]] = []

    # Web content (.txt)
    if web_dir.exists():
        for f in sorted(web_dir.glob("*.txt")):
            display = f"fafsa--{f.stem}.txt"
            files.append((f, display))

    # Transcripts (.txt) — already prefixed with fafsa--
    if transcript_dir.exists():
        for f in sorted(transcript_dir.glob("*.txt")):
            display = f.name  # already has fafsa-- prefix
            files.append((f, display))

    # PDFs — upload directly (Gemini accepts PDFs natively)
    if pdf_dir.exists():
        for f in sorted(pdf_dir.glob("*.pdf")):
            display = f"fafsa--{f.stem}.pdf"
            files.append((f, display))

    return files


def upload_to_store(
    client, store_name: str, files: list[tuple[Path, str]], max_workers: int = 10
) -> int:
    """Upload files to a Gemini File Search store with parallelism."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Get existing documents to avoid duplicates
    existing = set()
    for doc in client.file_search_stores.documents.list(parent=store_name):
        name = getattr(doc, "display_name", None)
        if name:
            existing.add(name)

    to_upload = [(f, d) for f, d in files if d not in existing]
    if not to_upload:
        print(f"  All {len(files)} files already uploaded, skipping.", flush=True)
        return 0

    print(
        f"  Uploading {len(to_upload)} new files ({len(existing)} fafsa files already exist)...",
        flush=True,
    )
    uploaded = 0

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(upload_one, client, store_name, f, d): d
            for f, d in to_upload
        }
        for i, future in enumerate(as_completed(futures), 1):
            name = futures[future]
            if future.result():
                uploaded += 1
                print(f"    [{i}/{len(to_upload)}] Uploaded {name}", flush=True)
            else:
                print(f"    [{i}/{len(to_upload)}] FAILED  {name}", flush=True)

    return uploaded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Ingest FAFSA content to all campus Gemini File Search stores"
    )
    parser.add_argument(
        "--campus",
        nargs="+",
        default=None,
        help="Campus codes to ingest (default: all 9)",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"External FAFSA source directory (default: {DEFAULT_SOURCE_DIR})",
    )
    return parser


def main():
    args = build_parser().parse_args()

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("ERROR: GEMINI_API_KEY or GOOGLE_API_KEY not set", flush=True)
        raise SystemExit(1)

    client = genai.Client(api_key=api_key)

    # Collect all FAFSA files once
    files = collect_files(
        web_dir=args.source_dir / "web",
        transcript_dir=args.source_dir / "transcripts",
        pdf_dir=args.source_dir / "pdf",
    )
    if not files:
        print(f"ERROR: No FAFSA files found under {args.source_dir}.", flush=True)
        raise SystemExit(1)

    print(f"Found {len(files)} FAFSA files to upload:", flush=True)
    for _, display in files:
        print(f"  {display}", flush=True)

    # Determine campuses
    if args.campus:
        codes = [c.upper() for c in args.campus]
    else:
        codes = sorted(CAMPUSES.keys())

    print(f"\nIngesting to {len(codes)} campus(es): {', '.join(codes)}\n", flush=True)
    summary = {}

    for code in codes:
        print(f"--- {code}: {CAMPUSES[code].name} ---", flush=True)
        store_display_name = f"{code.lower()}-chatbot-file-search"
        store_name = get_or_create_store(client, store_display_name)
        uploaded = upload_to_store(client, store_name, files)
        summary[code] = {"total_files": len(files), "uploaded": uploaded, "store": store_name}
        print(f"  Done: {uploaded} new uploads (of {len(files)} total)\n", flush=True)

    print("=== Summary ===", flush=True)
    print(json.dumps(summary, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
