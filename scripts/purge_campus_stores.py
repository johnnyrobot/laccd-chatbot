#!/usr/bin/env python3
"""Purge irrelevant documents from Gemini File Search stores for all 9 LACCD campuses.

Usage:
    # Dry run — report only, no deletions (all 9 campuses)
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/purge_campus_stores.py --dry-run

    # Purge specific campuses
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/purge_campus_stores.py --campus ELAC LACC

    # Purge all campuses
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/purge_campus_stores.py

    # Local files only (no Gemini API calls)
    PYTHONPATH="$PWD:$PWD/backend:$PYTHONPATH" python scripts/purge_campus_stores.py --local-only
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from shared.campus_config import CAMPUSES
from shared.content_filters import classify, get_purge_reason, should_purge

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "campus_crawls"
MAX_WORKERS = 5


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------

def _get_client():
    api_key = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key or not genai:
        return None
    return genai.Client(api_key=api_key)


def _find_store(client, campus_code: str) -> str | None:
    """Find the file search store for a campus by display name or env var."""
    env_key = f"GEMINI_FILE_SEARCH_STORE_{campus_code.upper()}"
    store_name = os.getenv(env_key)
    if store_name:
        return store_name
    target = f"{campus_code.lower()}-chatbot-file-search"
    try:
        for store in client.file_search_stores.list():
            if getattr(store, "display_name", None) == target:
                return store.name
    except Exception as exc:
        print(f"  WARNING: could not list stores for {campus_code}: {exc}")
    return None


def _delete_document(client, doc_name: str, display_name: str) -> bool:
    try:
        client.file_search_stores.documents.delete(
            name=doc_name,
            config=types.DeleteDocumentConfig(force=True),
        )
        return True
    except Exception as exc:
        print(f"    FAIL: {display_name} — {exc}")
        return False


# ---------------------------------------------------------------------------
# Per-campus processing
# ---------------------------------------------------------------------------

def purge_store(campus_code: str, *, dry_run: bool = False) -> dict:
    """Classify and optionally delete docs in a campus Gemini store.

    Returns summary dict with counts and breakdown.
    """
    client = _get_client()
    if client is None:
        print(f"  SKIP {campus_code}: Gemini client not available")
        return {}

    store_name = _find_store(client, campus_code)
    if not store_name:
        print(f"  SKIP {campus_code}: store not found")
        return {}

    print(f"  Listing documents in store {store_name}...")
    docs = list(client.file_search_stores.documents.list(parent=store_name))
    print(f"  Found {len(docs)} documents")

    purge_reasons: Counter[str] = Counter()
    priority_names: list[str] = []
    keep_count = 0
    to_delete: list[tuple[str, str]] = []  # (doc.name, display_name)

    for doc in docs:
        display_name = getattr(doc, "display_name", "") or ""
        doc_name = getattr(doc, "name", None)
        cat = classify(display_name)

        if cat == "priority":
            priority_names.append(display_name)
            keep_count += 1
        elif cat == "purge":
            reason = get_purge_reason(display_name) or "unknown"
            purge_reasons[reason] += 1
            if doc_name:
                to_delete.append((doc_name, display_name))
        else:
            keep_count += 1

    # Report
    print(f"\n  --- {campus_code} Report ---")
    print(f"  Total docs:  {len(docs)}")
    print(f"  To purge:    {len(to_delete)}")
    print(f"  To keep:     {keep_count}")
    print(f"  Priority:    {len(priority_names)}")
    if purge_reasons:
        print("  Purge breakdown:")
        for reason, count in purge_reasons.most_common():
            print(f"    {reason}: {count}")
    if priority_names:
        print(f"  Priority docs (sample): {priority_names[:5]}")

    # Delete
    deleted = 0
    if to_delete and not dry_run:
        print(f"\n  Deleting {len(to_delete)} documents ({MAX_WORKERS} workers)...")
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_delete_document, client, dn, disp): disp
                for dn, disp in to_delete
            }
            for future in as_completed(futures):
                if future.result():
                    deleted += 1
        print(f"  Deleted {deleted}/{len(to_delete)}")
    elif dry_run and to_delete:
        print(f"  [DRY RUN] Would delete {len(to_delete)} documents")

    return {
        "total": len(docs),
        "purged": deleted if not dry_run else 0,
        "would_purge": len(to_delete),
        "kept": keep_count,
        "priority": len(priority_names),
        "breakdown": dict(purge_reasons),
    }


def purge_local(campus_code: str, *, dry_run: bool = False) -> dict:
    """Delete local crawl files that match purge patterns."""
    crawl_dir = DATA_DIR / campus_code
    if not crawl_dir.is_dir():
        print(f"  SKIP {campus_code}: no local crawl dir at {crawl_dir}")
        return {}

    files = list(crawl_dir.glob("*.txt"))
    print(f"  Found {len(files)} local files in {crawl_dir}")

    purge_reasons: Counter[str] = Counter()
    to_delete: list[Path] = []

    for f in files:
        if should_purge(f.name):
            reason = get_purge_reason(f.name) or "unknown"
            purge_reasons[reason] += 1
            to_delete.append(f)

    deleted = 0
    if to_delete and not dry_run:
        for f in to_delete:
            f.unlink()
            deleted += 1
        print(f"  Deleted {deleted} local files")
    elif dry_run and to_delete:
        print(f"  [DRY RUN] Would delete {len(to_delete)} local files")

    if purge_reasons:
        print("  Local purge breakdown:")
        for reason, count in purge_reasons.most_common():
            print(f"    {reason}: {count}")

    return {
        "total": len(files),
        "purged": deleted if not dry_run else 0,
        "would_purge": len(to_delete),
        "kept": len(files) - len(to_delete),
        "breakdown": dict(purge_reasons),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Purge irrelevant docs from campus Gemini File Search stores",
    )
    parser.add_argument(
        "--campus",
        nargs="+",
        default=None,
        help="Campus codes (e.g. ELAC LACC). Default: all 9.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only, no deletions.",
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help="Only clean local crawl files, no Gemini API calls.",
    )
    args = parser.parse_args()

    codes = (
        [c.upper() for c in args.campus]
        if args.campus
        else list(CAMPUSES.keys())
    )

    mode = "DRY RUN" if args.dry_run else ("LOCAL ONLY" if args.local_only else "LIVE")
    print(f"=== Campus RAG Store Purge ({mode}) ===")
    print(f"Campuses: {', '.join(codes)}\n")

    summaries: dict[str, dict] = {}

    for code in codes:
        if code not in CAMPUSES:
            print(f"SKIP: unknown campus code '{code}'")
            continue

        print(f"\n{'='*50}")
        print(f"{code}: {CAMPUSES[code].name}")
        print(f"{'='*50}")

        if args.local_only:
            summaries[code] = purge_local(code, dry_run=args.dry_run)
        else:
            store_result = purge_store(code, dry_run=args.dry_run)
            local_result = purge_local(code, dry_run=args.dry_run)
            summaries[code] = {
                "store": store_result,
                "local": local_result,
            }

    # Final summary
    print(f"\n{'='*50}")
    print("SUMMARY")
    print(f"{'='*50}")
    for code, summary in summaries.items():
        if args.local_only:
            wp = summary.get("would_purge", 0)
            total = summary.get("total", 0)
            print(f"  {code}: {wp}/{total} files to purge")
        else:
            store = summary.get("store", {})
            wp = store.get("would_purge", 0)
            total = store.get("total", 0)
            print(f"  {code}: {wp}/{total} store docs to purge")


if __name__ == "__main__":
    main()
