#!/usr/bin/env python3
"""Convert campus catalog PDFs to clean markdown using Docling.

Output: data/catalog_md/{CAMPUS}/catalog.md
        data/catalog_md/{CAMPUS}/addendum-{slug}.md

Usage:
  PYTHONPATH=/app:/app/backend python scripts/convert_catalogs_docling.py
  PYTHONPATH=/app:/app/backend python scripts/convert_catalogs_docling.py --campus LASC
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "backend"))

from shared.campus_config import CAMPUSES  # noqa: E402

DEFAULT_SOURCE_DIR = PROJECT_DIR / "data" / "source_documents" / "catalogs"
OUT_DIR = PROJECT_DIR / "data" / "catalog_md"

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


def convert_one(converter, pdf_path: Path, out_path: Path) -> dict:
    """Convert a single PDF to markdown via Docling. Returns stats."""
    started = time.time()
    result = converter.convert(str(pdf_path))
    md = result.document.export_to_markdown()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    elapsed = time.time() - started
    return {
        "pdf": pdf_path.name,
        "out": str(out_path.relative_to(PROJECT_DIR)),
        "pdf_kb": pdf_path.stat().st_size // 1024,
        "md_chars": len(md),
        "md_kb": out_path.stat().st_size // 1024,
        "elapsed_secs": round(elapsed, 1),
    }


def slugify_addendum(name: str) -> str:
    base = Path(name).stem
    return "addendum-" + base.lower().replace(" ", "-").replace("_", "-")[:80] + ".md"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert catalog PDFs to markdown via Docling")
    parser.add_argument("--campus", nargs="+", default=None, help="Campus codes (default: all 9)")
    parser.add_argument("--catalog-only", action="store_true", help="Skip addendums")
    parser.add_argument("--addendums-only", action="store_true", help="Skip main catalog, only convert addendums")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=DEFAULT_SOURCE_DIR,
        help=f"External catalog directory (default: {DEFAULT_SOURCE_DIR})",
    )
    return parser


def main():
    args = build_parser().parse_args()
    addendum_dir = args.source_dir / "addendums"

    # Lazy-import so the script can at least show --help without docling installed
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    codes = [c.upper() for c in args.campus] if args.campus else list(CAMPUSES.keys())

    # LACCD catalogs are digital (text-layer) PDFs -- OCR adds 10-30x latency
    # and produces noise. Disable explicitly. Keep table-structure model on.
    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_ocr = False
    pipeline_options.do_table_structure = True
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
        }
    )
    summary = []

    for code in codes:
        if code not in CAMPUSES:
            print(f"unknown campus: {code}", flush=True)
            continue
        primary = args.source_dir / PRIMARY_CATALOG[code]
        if not primary.exists():
            print(f"  SKIP {code}: catalog not found at {primary}", flush=True)
            continue

        out_campus = OUT_DIR / code
        # Convert main catalog
        if not args.addendums_only:
            print(f"\n=== {code} catalog ({primary.name}, {primary.stat().st_size // 1024} KB) ===", flush=True)
            try:
                stats = convert_one(converter, primary, out_campus / "catalog.md")
                summary.append({"campus": code, "kind": "catalog", **stats})
                print(f"  -> {stats['md_kb']} KB markdown in {stats['elapsed_secs']}s", flush=True)
            except Exception as e:
                print(f"  ERROR {code} catalog: {e}", flush=True)
                summary.append({"campus": code, "kind": "catalog", "error": str(e)})

        # Convert addendums (if any)
        if not args.catalog_only and addendum_dir.exists():
            for adn in sorted(addendum_dir.glob(f"{code}-*.pdf")):
                print(f"--- {code} addendum ({adn.name}) ---", flush=True)
                out_md = out_campus / slugify_addendum(adn.name)
                try:
                    stats = convert_one(converter, adn, out_md)
                    summary.append({"campus": code, "kind": "addendum", **stats})
                    print(f"  -> {stats['md_kb']} KB markdown in {stats['elapsed_secs']}s", flush=True)
                except Exception as e:
                    print(f"  ERROR {code} addendum {adn.name}: {e}", flush=True)
                    summary.append({"campus": code, "kind": "addendum", "error": str(e)})

    print("\n=== Summary ===", flush=True)
    print(json.dumps(summary, indent=2, default=str), flush=True)


if __name__ == "__main__":
    main()
