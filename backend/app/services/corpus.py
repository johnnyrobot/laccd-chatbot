from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.config import settings
from app.models import DocumentRecord


STRIP_SELECTORS = [
    "#ocelot_ai",
    "[id^='ocelot']",
    "[aria-label='Chat Window']",
    ".MaxAI",
    ".ChatbotDrawer-conversation",
    ".MessagesWrapper-container",
    ".no-print",
    "script",
    "style",
    "nav[aria-labelledby='toc-heading']",
    "button",
    "footer",
]

CONTENT_SELECTORS = [
    "main article",
    "article",
    "#main-content",
    ".page-content",
    ".field--name-field-components",
    ".field--name-body",
    ".node__content",
    ".layout-content",
    ".region-content",
    ".content-main",
    ".main-content",
    "[role='main']",
    "main",
]

URL_META_SELECTORS = [
    ("link", {"rel": "canonical"}, "href"),
    ("meta", {"property": "og:url"}, "content"),
    ("meta", {"name": "twitter:url"}, "content"),
    ("link", {"rel": "alternate", "hreflang": "en"}, "href"),
]


_OLD_YEAR = re.compile(r"(?:19\d{2}|200\d|201\d)")


def _is_pre_2020(filename: str) -> bool:
    """Return True if filename contains only pre-2020 year tokens."""
    years = _OLD_YEAR.findall(filename)
    return bool(years) and all(int(y) < 2020 for y in years)


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return cleaned.strip("-") or "document"


def is_http_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def is_lamission_url(value: str | None) -> bool:
    if not is_http_url(value):
        return False
    host = (urlparse(value).hostname or "").lower()
    return host == "lamission.edu" or host == "www.lamission.edu" or host.endswith(".lamission.edu")


def normalize_lamission_url(value: str | None, *, base_url: str | None = None) -> str | None:
    if not value:
        return None
    resolved = urljoin(base_url or "https://www.lamission.edu/", value).strip()
    if not is_lamission_url(resolved):
        return None
    parsed = urlparse(resolved)
    if parsed.scheme != "https":
        parsed = parsed._replace(scheme="https")
    normalized = parsed._replace(fragment="", params="", query="")
    path = normalized.path or "/"
    if path != "/":
        path = path.rstrip("/")
    normalized = normalized._replace(path=path)
    final = normalized.geturl()
    lowered = final.lower()
    if lowered.endswith(".pdf") or lowered.startswith("mailto:") or lowered.startswith("tel:"):
        return None
    if "/~" in lowered:
        return None
    return final


def _extract_source_url(soup: BeautifulSoup, fallback_url: str | None = None) -> str | None:
    for tag_name, attrs, attr_name in URL_META_SELECTORS:
        node = soup.find(tag_name, attrs=attrs)
        if node and node.has_attr(attr_name):
            value = node[attr_name].strip()
            normalized = normalize_lamission_url(value, base_url=fallback_url)
            if normalized:
                return normalized
    for anchor in soup.find_all("a", href=True):
        normalized = normalize_lamission_url(anchor["href"].strip(), base_url=fallback_url)
        if normalized:
            return normalized
    return normalize_lamission_url(fallback_url)


def _pick_content_node(soup: BeautifulSoup):
    best_node = None
    best_text_len = 0
    for selector in CONTENT_SELECTORS:
        for node in soup.select(selector):
            text = node.get_text("\n", strip=True)
            if len(text) > best_text_len:
                best_text_len = len(text)
                best_node = node
    if best_node is not None and best_text_len >= 250:
        return best_node
    body = soup.body or soup
    candidates = []
    for node in body.find_all(["section", "div", "article", "main"], recursive=True):
        text = node.get_text("\n", strip=True)
        if len(text) >= 250:
            candidates.append((len(text), node))
    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]
    return body


def _clean_html(html: str, fallback_url: str | None = None) -> tuple[str, str, list[str], str | None, str]:
    soup = BeautifulSoup(html, "lxml")
    for selector in STRIP_SELECTORS:
        for node in soup.select(selector):
            node.decompose()

    article = _pick_content_node(soup)
    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.replace("ADA Version —", "").strip()
    h1 = article.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)

    headings = [
        heading.get_text(" ", strip=True)
        for heading in article.find_all(["h1", "h2", "h3"])
        if heading.get_text(strip=True)
    ]
    source_url = _extract_source_url(soup, fallback_url=fallback_url)
    text = article.get_text("\n", strip=True)
    text = re.sub(r"\n{2,}", "\n\n", text).strip()
    return str(article), text, headings, source_url, title


def extract_internal_links(html: str, *, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    content_root = _pick_content_node(soup)
    links: list[str] = []
    seen: set[str] = set()
    for anchor in content_root.find_all("a", href=True):
        normalized = normalize_lamission_url(anchor["href"], base_url=base_url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
    return links


def extract_text_from_html(html: str, *, fallback_url: str | None = None) -> str:
    return _clean_html(html, fallback_url=fallback_url)[1]


def load_manifest() -> list[DocumentRecord]:
    if not settings.manifest_path.exists():
        return []
    if settings.manifest_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("manifest exceeds byte limit")
    records: list[DocumentRecord] = []
    with settings.manifest_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(DocumentRecord.model_validate(json.loads(line)))
                if len(records) > 1_000:
                    raise ValueError("manifest exceeds record limit")
    return records


def _record_key(record: DocumentRecord) -> str:
    if record.source_type == "crawl4ai" and record.source_url and is_http_url(record.source_url):
        return f"url:{record.source_url}"
    return f"{record.source_type}:path:{record.local_path}"


class CorpusNormalizer:
    MAX_HTML_FILES = 1_000
    MAX_HTML_FILE_BYTES = 2 * 1024 * 1024
    MAX_HTML_TOTAL_BYTES = 256 * 1024 * 1024
    MAX_RETAINED_RECORDS = 1_000
    MAX_RETAINED_BYTES = 256 * 1024 * 1024
    def __init__(self, corpus_dir: Path | None = None) -> None:
        self.corpus_dir = corpus_dir or settings.corpus_dir
        self.normalized_dir = settings.normalized_dir
        self.crawl_raw_dir = settings.crawl4ai_raw_dir
        self.crawl_normalized_dir = settings.crawl4ai_normalized_dir
        self.manifest_path = settings.manifest_path

    def iter_html_files(self) -> Iterable[Path]:
        return sorted(self.corpus_dir.glob("*.html"))[: self.MAX_HTML_FILES]

    def normalize(self) -> list[DocumentRecord]:
        records = []
        total_bytes = 0
        for path in self.iter_html_files():
            if _is_pre_2020(path.name):
                continue
            size = path.stat().st_size
            if size > self.MAX_HTML_FILE_BYTES:
                logger.warning("Skipping oversized corpus HTML: %s", path.name)
                continue
            total_bytes += size
            if total_bytes > self.MAX_HTML_TOTAL_BYTES:
                logger.warning("Corpus HTML aggregate limit reached")
                break
            record = self._record_from_html(
                html=path.read_text(encoding="utf-8", errors="ignore"),
                source_type="preview_html",
                local_path=path,
                output_dir=self.normalized_dir,
            )
            records.append(record)
        return self.merge_records(records)

    def persist_crawl_result(
        self,
        url: str,
        html: str,
        title_hint: str | None = None,
        markdown_hint: str | None = None,
    ) -> DocumentRecord:
        if len(html.encode("utf-8")) > self.MAX_HTML_FILE_BYTES:
            raise ValueError("crawl result exceeds HTML byte limit")
        if markdown_hint and len(markdown_hint.encode("utf-8")) > self.MAX_HTML_FILE_BYTES:
            raise ValueError("crawl result exceeds markdown byte limit")
        parsed = urlparse(url)
        stem = slugify(parsed.path.strip("/") or parsed.netloc or "crawl4ai-page")
        raw_path = self.crawl_raw_dir / f"{stem}.html"
        raw_path.write_text(html, encoding="utf-8")
        record = self._record_from_html(
            html=html,
            source_type="crawl4ai",
            local_path=raw_path,
            output_dir=self.crawl_normalized_dir,
            fallback_url=url,
            title_hint=title_hint,
            markdown_hint=markdown_hint,
        )
        try:
            self.merge_records([record])
        except Exception:
            for path_value in (raw_path, Path(record.text_path), Path(record.normalized_path)):
                path_value.unlink(missing_ok=True)
            raise
        return record

    def merge_records(self, new_records: list[DocumentRecord]) -> list[DocumentRecord]:
        merged = {_record_key(record): record for record in load_manifest()}
        for record in new_records:
            merged[_record_key(record)] = record
        final_records = sorted(
            merged.values(),
            key=lambda record: (record.source_type, record.title.lower(), record.doc_id),
        )
        if len(final_records) > self.MAX_RETAINED_RECORDS:
            raise ValueError("retained corpus exceeds record limit")
        retained_bytes = 0
        for record in final_records:
            for path_value in (record.local_path, record.text_path, record.normalized_path):
                path = Path(path_value)
                if path.is_file():
                    retained_bytes += path.stat().st_size
                    if retained_bytes > self.MAX_RETAINED_BYTES:
                        raise ValueError("retained corpus exceeds byte limit")
        self._write_manifest(final_records)
        return final_records

    def candidate_crawl_urls(self, limit: int = 50) -> list[str]:
        urls = []
        seen = set()
        priority_patterns = [
            "/about/",
            "/academics/",
            "/academic-services/",
            "/calendar",
            "/campus-life/",
            "/financial-aid",
            "/cost-tuition/",
            "/schedules",
            "/catalog",
            "/admissions",
            "/student-services/",
            "/offices/",
        ]
        for record in load_manifest():
            if not is_lamission_url(record.source_url):
                continue
            url = record.source_url.rstrip("/")
            if url.lower().endswith(".pdf"):
                continue
            if "/~" in url or url.startswith("http://"):
                continue
            if url not in seen:
                seen.add(url)
                urls.append(url)
        seed_urls = [
            "https://www.lamission.edu/",
            "https://www.lamission.edu/admissions",
            "https://www.lamission.edu/admissions/how-to-apply",
            "https://www.lamission.edu/student-services/counseling",
            "https://www.lamission.edu/about/parking",
            "https://www.lamission.edu/offices/business-office/parking-permit",
            "https://www.lamission.edu/financial-aid",
            "https://www.lamission.edu/financial-aid/apply",
            "https://www.lamission.edu/cost-tuition/tuition-fees",
            "https://www.lamission.edu/academic-services/learning-center",
            "https://www.lamission.edu/academic-services/library",
            "https://www.lamission.edu/student-services/transfer-center",
            "https://www.lamission.edu/student-services/spr/eops",
        ]
        for url in reversed(seed_urls):
            if url not in seen:
                urls.insert(0, url)
        urls.sort(
            key=lambda url: (
                0 if any(pattern in url.lower() for pattern in priority_patterns) else 1,
                len(url),
                url,
            )
        )
        return urls[:limit]

    def _record_from_html(
        self,
        html: str,
        source_type: str,
        local_path: Path,
        output_dir: Path,
        fallback_url: str | None = None,
        title_hint: str | None = None,
        markdown_hint: str | None = None,
    ) -> DocumentRecord:
        clean_html, text, headings, source_url, parsed_title = _clean_html(html, fallback_url=fallback_url)
        if markdown_hint and len(markdown_hint.strip()) > len(text.strip()) and len(markdown_hint.strip()) >= 250:
            text = markdown_hint.strip()
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        doc_stem = slugify(local_path.stem)
        normalized_path = output_dir / f"{doc_stem}.html"
        text_path = output_dir / f"{doc_stem}.txt"
        normalized_path.write_text(clean_html, encoding="utf-8")
        text_path.write_text(text, encoding="utf-8")
        title = headings[0] if headings else title_hint or parsed_title or local_path.stem
        return DocumentRecord(
            doc_id=doc_stem,
            title=title,
            source_url=source_url,
            local_path=str(local_path),
            source_type=source_type,
            content_hash=content_hash,
            last_ingested_at=datetime.now(UTC),
            normalized_path=str(normalized_path),
            text_path=str(text_path),
            headings=headings,
            text_excerpt=text[:280],
        )

    def _write_manifest(self, records: list[DocumentRecord]) -> None:
        with self.manifest_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(record.model_dump_json())
                handle.write("\n")
