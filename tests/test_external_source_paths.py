from __future__ import annotations

from pathlib import Path

import pytest

from scripts import convert_catalogs_docling
from scripts import ingest_catalog_pdfs
from scripts import ingest_fafsa_corpus
from scripts import index_addenda
from scripts import parse_catalog_pdf


@pytest.mark.parametrize(
    "module",
    (
        convert_catalogs_docling,
        ingest_catalog_pdfs,
        ingest_fafsa_corpus,
        index_addenda,
        parse_catalog_pdf,
    ),
)
def test_ingestion_commands_accept_an_external_source_directory(
    module,
    tmp_path: Path,
) -> None:
    args = module.build_parser().parse_args(["--source-dir", str(tmp_path)])

    assert args.source_dir == tmp_path


def test_fafsa_collection_uses_the_selected_source_tree(tmp_path: Path) -> None:
    web_dir = tmp_path / "web"
    transcript_dir = tmp_path / "transcripts"
    pdf_dir = tmp_path / "pdf"
    web_dir.mkdir()
    transcript_dir.mkdir()
    pdf_dir.mkdir()
    (web_dir / "application.txt").write_text("web", encoding="utf-8")
    (transcript_dir / "fafsa--video.txt").write_text("video", encoding="utf-8")
    (pdf_dir / "guide.pdf").write_bytes(b"%PDF-1.4\n")

    files = ingest_fafsa_corpus.collect_files(
        web_dir=web_dir,
        transcript_dir=transcript_dir,
        pdf_dir=pdf_dir,
    )

    assert files == [
        (web_dir / "application.txt", "fafsa--application.txt"),
        (transcript_dir / "fafsa--video.txt", "fafsa--video.txt"),
        (pdf_dir / "guide.pdf", "fafsa--guide.pdf"),
    ]
