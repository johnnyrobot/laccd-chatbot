#!/usr/bin/env python3
"""Crawl all 9 LACCD campus websites with crawl4ai.

Sitemap-first discovery with BFS fallback, FAQ-tuned URL filters, per-campus
markdown output compatible with scripts/ingest_campus_corpus.py.

Usage:
    PYTHONPATH=/app:/app/backend python scripts/crawl_campuses_c4a.py
    PYTHONPATH=/app:/app/backend python scripts/crawl_campuses_c4a.py --campus LASC
    PYTHONPATH=/app:/app/backend python scripts/crawl_campuses_c4a.py --bfs-only
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR / "backend"))

from shared.campus_config import CAMPUSES  # noqa: E402

DATA_DIR = PROJECT_DIR / "data" / "campus_crawls"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
MAIN_CONTENT_SELECTOR = "main, #main, [role=main], .main-content, article, .content-main, .region-content"

ALLOW_PATTERNS = (
    "academic", "program", "department", "/aos", "admission", "financial-aid",
    "registration", "enroll", "student-service", "counsel", "tutor",
    "transfer", "learning-center", "library", "career",
    "about", "contact", "hours", "office", "faq", "help",
    "getting-started", "welcome", "orientation",
    "cost", "tuition", "fees", "payment", "parking",
    "dsps", "disab", "eops", "calwork", "veteran", "basic-needs",
    "food-pantry", "health", "wellness",
    "international", "dual-enroll", "internship", "work-study",
)
BLOCK_PATTERNS = (
    "/news", "/blog", "/press", "/event", "/calendar", "/gallery",
    "/photo", "/media-library", "/feed", "/rss",
    "/login", "/register-account", "/account",
    "/archive", "/old/", "/legacy", "/taxonomy",
    "/people/", "/faculty/", "/staff-profile",
    "/board/agenda", "/minutes",
    "/print", "/search", "/node/", "accreditation/docs",
)
BINARY_RE = re.compile(
    r"\.(pdf|zip|docx?|xlsx?|pptx?|jpg|jpeg|png|gif|mp4|webm|mp3|wav|ico|svg|webp|css|js|woff2?|ttf|otf)(\?.*)?$",
    re.IGNORECASE,
)
SEED_PATHS = (
    "/", "/academics", "/admissions", "/financial-aid",
    "/student-services", "/about", "/programs", "/departments",
)

MIN_PAGE_CHARS = 200
MAX_REMOTE_PAGE_BYTES = 2 * 1024 * 1024
MAX_CAMPUS_BYTES = 256 * 1024 * 1024
#: Minimum fraction of fetched pages that must survive extraction before the run
#: is treated as a failure. Several bugs have produced a clean exit with a
#: near-zero keep rate; this makes that loud instead of silent.
DEFAULT_MIN_YIELD = 0.5
WALL_CLOCK_CAP_SECS = 4 * 3600
# Concurrency MUST be 1: crawl4ai shares a browser context across concurrent
# arun() calls, and LASC (likely others) end up serving the chatbot widget
# stub page (~75 chars) instead of full content when 2+ requests overlap.
# Sequential gives the real page (~50KB-200KB markdown).
WITHIN_CAMPUS_CONCURRENCY = 1
POLITE_DELAY_SECS = 1.0
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemaps/sitemap.xml")
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
MAX_SITEMAP_BYTES = 2 * 1024 * 1024
MAX_SITEMAP_URLS = 5_000


def normalize_url(url: str, base_url: str) -> str | None:
    if not url:
        return None
    full = urljoin(base_url, url.strip())
    parsed = urlparse(full)
    if parsed.scheme not in ("http", "https"):
        return None
    path = parsed.path or "/"
    cleaned = urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))
    if len(cleaned) > 1 and cleaned.endswith("/"):
        cleaned = cleaned.rstrip("/")
    return cleaned


def is_same_domain(url: str, allowed_hosts: set[str]) -> bool:
    return urlparse(url).netloc.lower() in allowed_hosts


def passes_filters(url: str) -> bool:
    if BINARY_RE.search(url):
        return False
    path = urlparse(url).path.lower()
    if any(b in path for b in BLOCK_PATTERNS):
        return False
    if path in ("/", ""):
        return True
    return any(a in path for a in ALLOW_PATTERNS)


async def fetch_sitemap_urls(base: str, allowed_hosts: set[str]) -> list[str]:
    found: list[str] = []
    async with httpx.AsyncClient(
        timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        for path in SITEMAP_PATHS:
            try:
                async with client.stream("GET", base + path) as r:
                    if r.status_code != 200:
                        continue
                    final_url = str(r.url)
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_SITEMAP_BYTES:
                            raise ValueError("sitemap exceeds byte limit")
            except Exception:
                continue
            if urlparse(final_url).scheme != "https" or not is_same_domain(final_url, allowed_hosts):
                continue
            try:
                root = ET.fromstring(bytes(body))
            except ET.ParseError:
                continue
            sub_locs = [s.text for s in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS) if s.text]
            if sub_locs:
                for sm_url in sub_locs[:50]:
                    try:
                        normalized_sitemap = normalize_url(sm_url.strip(), base)
                        if (
                            not normalized_sitemap
                            or urlparse(normalized_sitemap).scheme != "https"
                            or not is_same_domain(normalized_sitemap, allowed_hosts)
                        ):
                            continue
                        async with client.stream("GET", normalized_sitemap) as sub:
                            if sub.status_code != 200:
                                continue
                            sub_final = str(sub.url)
                            sub_body = bytearray()
                            async for chunk in sub.aiter_bytes():
                                sub_body.extend(chunk)
                                if len(sub_body) > MAX_SITEMAP_BYTES:
                                    raise ValueError("nested sitemap exceeds byte limit")
                        if urlparse(sub_final).scheme != "https" or not is_same_domain(sub_final, allowed_hosts):
                            continue
                        sub_root = ET.fromstring(bytes(sub_body))
                        for u in sub_root.findall(".//sm:url/sm:loc", SITEMAP_NS):
                            if u.text:
                                if len(found) < MAX_SITEMAP_URLS:
                                    found.append(u.text.strip())
                    except Exception:
                        continue
            else:
                for u in root.findall(".//sm:url/sm:loc", SITEMAP_NS):
                    if u.text:
                        if len(found) < MAX_SITEMAP_URLS:
                            found.append(u.text.strip())
            if found:
                break

    seen: set[str] = set()
    deduped: list[str] = []
    for raw in found:
        norm = normalize_url(raw, base)
        if (
            norm
            and urlparse(norm).scheme == "https"
            and norm not in seen
            and is_same_domain(norm, allowed_hosts)
        ):
            seen.add(norm)
            deduped.append(norm)
    return deduped


class _LinkExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


async def bfs_discover(base: str, allowed_hosts: set[str], depth: int = 4, max_urls: int = 2000) -> list[str]:
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque()
    found: list[str] = []
    for path in SEED_PATHS:
        norm = normalize_url(base + path, base)
        if norm and norm not in seen:
            seen.add(norm)
            queue.append((norm, 0))
            found.append(norm)

    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        while queue and len(found) < max_urls:
            url, d = queue.popleft()
            if d >= depth:
                continue
            try:
                r = await client.get(url)
            except Exception:
                continue
            if r.status_code != 200:
                continue
            if urlparse(str(r.url)).scheme != "https" or not is_same_domain(str(r.url), allowed_hosts):
                continue
            parser = _LinkExtractor()
            try:
                parser.feed(r.text)
            except Exception:
                continue
            for href in parser.links:
                norm = normalize_url(href, url)
                if norm and norm not in seen and is_same_domain(norm, allowed_hosts):
                    seen.add(norm)
                    found.append(norm)
                    queue.append((norm, d + 1))
                    if len(found) >= max_urls:
                        break
    return found


def url_to_filename(url: str) -> str:
    """Build a collision-free output filename for a crawled URL.

    Every distinct URL must map to a distinct filename. Two earlier versions did
    not, and both lost pages silently:

    * dropping the query string and truncating the path at 200 chars made
      sibling URLs collapse (LAPC logged kept=944 but wrote 828 files);
    * returning a bare "homepage.txt" for any root URL collapsed the scheme and
      host variants a single campus legitimately yields — LAPC crawled both
      http://www.piercecollege.edu and https://www.piercecollege.edu, so one
      overwrote the other (kept=943, 942 files).

    The digest of the full URL is therefore appended unconditionally, root URLs
    included, and covers scheme, host, path and query.
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    raw = f"{path}?{parsed.query}" if parsed.query else path
    stem = re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_") or "homepage"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{stem[:180]}_{digest}.txt"


async def crawl_campus(code: str, campus, *, bfs_only: bool = False) -> dict:
    out_dir = DATA_DIR / code
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / ".crawled.txt"

    done_urls: set[str] = set()
    if manifest_path.exists():
        done_urls = set(line.strip() for line in manifest_path.read_text().splitlines() if line.strip())

    # removeprefix, not lstrip: lstrip takes a *character set*, so
    # "www.wlac.edu".lstrip("www.") returns "lac.edu" — it eats the leading "w"
    # of "wlac" too. That pointed the WLAC crawl at the nonexistent
    # www.lac.edu, which discovered 8 URLs and kept 0.
    domain = campus.website.removeprefix("www.").rstrip("/")
    base = f"https://www.{domain}"
    allowed_hosts = {f"www.{domain}", domain}

    print(f"\n=== {code} ({campus.name}) ===", flush=True)
    print(f"  base={base} hosts={allowed_hosts}", flush=True)

    urls: list[str] = []
    if not bfs_only:
        urls = await fetch_sitemap_urls(base, allowed_hosts)
        print(f"  sitemap discovery: {len(urls)} URLs", flush=True)
    if bfs_only or len(urls) < 50:
        bfs_urls = await bfs_discover(base, allowed_hosts, depth=4, max_urls=2000)
        merged = list({*urls, *bfs_urls})
        print(f"  bfs discovery: {len(bfs_urls)} URLs (merged total: {len(merged)})", flush=True)
        urls = merged

    filtered = [u for u in urls if passes_filters(u)]
    print(f"  after allow/block filters: {len(filtered)} URLs", flush=True)

    to_crawl = [u for u in filtered if u not in done_urls]
    print(f"  after resume skip: {len(to_crawl)} to fetch", flush=True)
    if not to_crawl:
        return {
            "campus": code, "discovered": len(urls), "filtered": len(filtered),
            "fetched": 0, "kept": 0, "elapsed_secs": 0,
        }

    browser_config = BrowserConfig(
        headless=True, verbose=False, browser_type="chromium",
        viewport_width=1440, viewport_height=900,
        user_agent=USER_AGENT,
    )
    run_config = CrawlerRunConfig(
        page_timeout=60000,
        wait_until="networkidle",
        delay_before_return_html=2.0,
        # remove_overlay_elements MUST stay False. With the installed crawl4ai
        # it strips the entire document rather than just modals/cookie banners:
        # on https://www.elac.edu/academics it takes the extracted HTML from
        # 81,243 chars to 43 and the markdown to 0, so every page falls under
        # MIN_PAGE_CHARS and the crawl silently keeps nothing (fetched=250
        # kept=0). Re-enable only with a per-campus before/after char count.
        remove_overlay_elements=False,
        css_selector=MAIN_CONTENT_SELECTOR,
        word_count_threshold=10,
        cache_mode="bypass",
        verbose=False,
    )

    started = time.time()
    fetched = 0
    kept = 0
    kept_bytes = sum(
        path.stat().st_size
        for path in out_dir.glob("*.txt")
        if path != manifest_path and path.is_file()
    )
    if kept_bytes > MAX_CAMPUS_BYTES:
        raise RuntimeError(
            f"{code}: retained campus crawl already exceeds {MAX_CAMPUS_BYTES} bytes"
        )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        for url in to_crawl:
            if time.time() - started > WALL_CLOCK_CAP_SECS:
                print(f"    [{code}] wall-clock cap hit, stopping", flush=True)
                break
            await asyncio.sleep(POLITE_DELAY_SECS)
            try:
                result = await crawler.arun(url=url, config=run_config)
            except Exception as e:
                print(f"    [{code}] EXC {url}: {e}", flush=True)
                continue
            fetched += 1
            md = (str(result.markdown) if getattr(result, "markdown", None) else "").strip()
            if result.success and len(md) >= MIN_PAGE_CHARS:
                encoded_size = len(md.encode("utf-8"))
                if encoded_size > MAX_REMOTE_PAGE_BYTES:
                    raise RuntimeError(f"{code}: page exceeds {MAX_REMOTE_PAGE_BYTES} bytes: {url}")
                if kept_bytes + encoded_size > MAX_CAMPUS_BYTES:
                    raise RuntimeError(f"{code}: campus crawl exceeds {MAX_CAMPUS_BYTES} bytes")
                fname = url_to_filename(url)
                (out_dir / fname).write_text(md, encoding="utf-8")
                kept += 1
                kept_bytes += encoded_size
            with manifest_path.open("a", encoding="utf-8") as f:
                f.write(url + "\n")
            if fetched % 25 == 0:
                elapsed = time.time() - started
                print(
                    f"    [{code}] fetched={fetched}/{len(to_crawl)} kept={kept} elapsed={elapsed:.0f}s",
                    flush=True,
                )

    elapsed = time.time() - started
    print(f"  done {code}: fetched={fetched} kept={kept} elapsed={elapsed:.0f}s", flush=True)
    return {
        "campus": code, "discovered": len(urls), "filtered": len(filtered),
        "fetched": fetched, "kept": kept, "elapsed_secs": int(elapsed),
    }


async def main():
    parser = argparse.ArgumentParser(description="LACCD campus crawler (crawl4ai, FAQ-focused)")
    parser.add_argument("--campus", nargs="+", default=None, help="Campus codes (default: all 9)")
    parser.add_argument("--bfs-only", action="store_true", help="Skip sitemap, BFS only")
    parser.add_argument(
        "--min-yield",
        type=float,
        default=DEFAULT_MIN_YIELD,
        help=(
            "Fail (exit 1) if any campus keeps less than this fraction of the pages "
            f"it fetched. Default {DEFAULT_MIN_YIELD}; pass 0 to disable."
        ),
    )
    args = parser.parse_args()

    codes = [c.upper() for c in args.campus] if args.campus else list(CAMPUSES.keys())
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    summary = []
    for code in codes:
        if code not in CAMPUSES:
            print(f"unknown campus: {code}", flush=True)
            continue
        summary.append(await crawl_campus(code, CAMPUSES[code], bfs_only=args.bfs_only))

    print("\n=== FINAL SUMMARY ===", flush=True)
    for s in summary:
        print(s, flush=True)

    # A crawl that fetches pages but keeps none exits 0 and looks successful:
    # remove_overlay_elements stripped every document (fetched=250 kept=0) and the
    # www-stripping bug pointed WLAC at a dead host (fetched=8 kept=0). Both ran
    # to completion without an error. Turn that into a non-zero exit.
    failures = []
    for s in summary:
        fetched, kept = s.get("fetched", 0), s.get("kept", 0)
        if not fetched:
            # Nothing fetched is legitimate when every URL was resume-skipped,
            # but not when discovery found work to do.
            if s.get("filtered"):
                failures.append(f"{s['campus']}: filtered={s['filtered']} but fetched 0")
            continue
        ratio = kept / fetched
        if args.min_yield > 0 and ratio < args.min_yield:
            failures.append(
                f"{s['campus']}: kept {kept}/{fetched} = {ratio:.0%} "
                f"(below {args.min_yield:.0%})"
            )

    if failures:
        print("\nFAILED yield check:", flush=True)
        for f in failures:
            print(f"  {f}", flush=True)
        print(
            "\nA low keep rate usually means extraction is broken, not that the sites "
            "are empty — check the content selector, remove_overlay_elements, and the "
            "resolved base URL. Re-run with --min-yield 0 to override.",
            flush=True,
        )
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
