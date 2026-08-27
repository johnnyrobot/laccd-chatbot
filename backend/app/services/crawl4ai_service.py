from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.services.corpus import (
    CorpusNormalizer,
    extract_internal_links,
    extract_text_from_html,
    is_lamission_url,
    normalize_lamission_url,
)

logger = logging.getLogger(__name__)
MAX_SITEMAP_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
except Exception:  # pragma: no cover
    AsyncWebCrawler = None
    BrowserConfig = None
    CrawlerRunConfig = None

try:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright
except Exception:  # pragma: no cover
    PlaywrightTimeoutError = Exception
    async_playwright = None


ELLIOTT_EXCLUDED_SELECTOR = "#ocelot_ai, [id^='ocelot'], [aria-label='Chat Window'], .MaxAI"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class CrawlResult:
    url: str
    markdown: str
    html: str
    title: str | None
    links: list[str]
    capture_method: str


class Crawl4AIService:
    def __init__(self) -> None:
        self.normalizer = CorpusNormalizer()

    def is_available(self) -> bool:
        return AsyncWebCrawler is not None

    def browser_fallback_available(self) -> bool:
        return async_playwright is not None

    async def crawl_live_page(self, url: str) -> CrawlResult:
        normalized_url = normalize_lamission_url(url) or url
        if not self.is_available():
            raise RuntimeError("crawl4ai is not installed or unavailable in this environment")

        primary = await self._crawl_with_crawl4ai(normalized_url)
        if self._is_useful_capture(primary):
            return primary

        if self.browser_fallback_available():
            logger.info("Falling back to browser capture for %s", normalized_url)
            fallback = await self._crawl_with_browser(normalized_url)
            if self._is_useful_capture(fallback):
                return fallback
            if len(fallback.html) > len(primary.html):
                return fallback
        return primary

    async def discover_seed_urls(self, limit: int = 10) -> list[str]:
        sitemap_url = "https://www.lamission.edu/sitemap.xml"
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                async with client.stream("GET", sitemap_url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_SITEMAP_BYTES:
                            raise ValueError("sitemap exceeds byte limit")
            root = ET.fromstring(bytes(body))
            namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            urls = [
                normalize_lamission_url(node.text)
                for node in root.findall(".//sm:loc", namespace)
                if node.text
            ]
            return [url for url in urls if url][:limit]
        except Exception as exc:
            logger.warning("Failed to read sitemap, using manifest-derived URLs: %s", exc)
            return self.normalizer.candidate_crawl_urls(limit=limit)

    async def discover_site_urls(self, limit: int = 50, seed_urls: list[str] | None = None) -> list[str]:
        seeds = seed_urls or await self.discover_seed_urls(limit=min(limit, 20))
        queue = deque(normalize_lamission_url(url) for url in seeds)
        visited: set[str] = set()
        discovered: list[str] = []
        max_visits = max(limit * 4, len(seeds))

        while queue and len(discovered) < limit and len(visited) < max_visits:
            current = queue.popleft()
            if not current or current in visited:
                continue
            visited.add(current)
            try:
                result = await self.crawl_live_page(current)
            except Exception as exc:  # pragma: no cover
                logger.warning("Live crawl failed for %s: %s", current, exc)
                continue
            discovered.append(result.url)
            for link in result.links:
                if link not in visited and link not in queue:
                    queue.append(link)
        return discovered

    async def crawl_and_persist(self, urls: list[str], limit: int = 10) -> list[dict]:
        seeds = [normalize_lamission_url(url) for url in urls if normalize_lamission_url(url)] or await self.discover_seed_urls(
            limit=min(limit, 20)
        )
        queue = deque(seeds)
        visited: set[str] = set()
        accepted_records = []
        results = []
        max_visits = max(limit * 5, len(seeds))

        while queue and len(results) < limit and len(visited) < max_visits:
            url = queue.popleft()
            if not url or url in visited or not is_lamission_url(url):
                continue
            visited.add(url)

            try:
                crawl = await self.crawl_live_page(url)
            except Exception as exc:  # pragma: no cover
                logger.warning("Unable to crawl %s: %s", url, exc)
                continue

            for link in crawl.links:
                if link not in visited and link not in queue:
                    queue.append(link)

            record = self.normalizer.persist_crawl_result(
                url=crawl.url,
                html=crawl.html,
                title_hint=crawl.title,
                markdown_hint=self._usable_markdown(crawl.markdown),
            )
            text_length = len(Path(record.text_path).read_text(encoding="utf-8", errors="ignore").strip())
            if text_length < 250:
                logger.info("Skipping low-content crawl result for %s", url)
                continue
            accepted_records.append(record)
            results.append(
                {
                    "url": crawl.url,
                    "title": crawl.title,
                    "doc_id": record.doc_id,
                    "text_path": record.text_path,
                    "normalized_path": record.normalized_path,
                    "text_excerpt": record.text_excerpt,
                    "capture_method": crawl.capture_method,
                    "discovered_links": len(crawl.links),
                }
            )

        if accepted_records:
            self.normalizer.merge_records(accepted_records)
        return results

    async def _crawl_with_crawl4ai(self, url: str) -> CrawlResult:
        browser_config = BrowserConfig(
            headless=True,
            enable_stealth=True,
            verbose=False,
            viewport_width=1440,
            viewport_height=900,
            user_agent=DEFAULT_USER_AGENT,
        )
        run_config = CrawlerRunConfig(
            page_timeout=60000,
            wait_until="networkidle",
            delay_before_return_html=2.0,
            remove_overlay_elements=True,
            scan_full_page=True,
            magic=True,
            simulate_user=True,
            override_navigator=True,
            excluded_selector=ELLIOTT_EXCLUDED_SELECTOR,
        )
        async with AsyncWebCrawler(config=browser_config) as crawler:
            result = await crawler.arun(url=url, config=run_config)
        html = result.html or ""
        markdown = str(result.markdown or "")
        if len(html.encode("utf-8")) > MAX_CAPTURE_BYTES or len(markdown.encode("utf-8")) > MAX_CAPTURE_BYTES:
            raise ValueError("crawl capture exceeds byte limit")
        return CrawlResult(
            url=url,
            markdown=markdown,
            html=html,
            title=(result.metadata or {}).get("title"),
            links=extract_internal_links(html, base_url=url),
            capture_method="crawl4ai",
        )

    async def _crawl_with_browser(self, url: str) -> CrawlResult:
        if async_playwright is None:
            raise RuntimeError("playwright is unavailable in this environment")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            page = await browser.new_page(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1440, "height": 900},
            )
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_load_state("networkidle", timeout=30000)
                await page.wait_for_selector("main", timeout=30000)
                await page.wait_for_timeout(1500)
                await page.evaluate(
                    """
                    () => {
                      const selectors = [
                        '#ocelot_ai',
                        '[id^="ocelot"]',
                        '[aria-label="Chat Window"]',
                        '.MaxAI'
                      ];
                      for (const selector of selectors) {
                        for (const node of document.querySelectorAll(selector)) {
                          node.remove();
                        }
                      }
                    }
                    """
                )
                html = await page.content()
                title = await page.title()
            except PlaywrightTimeoutError:
                html = await page.content()
                title = await page.title()
            finally:
                await page.close()
                await browser.close()

        if len(html.encode("utf-8")) > MAX_CAPTURE_BYTES:
            raise ValueError("browser capture exceeds byte limit")

        return CrawlResult(
            url=url,
            markdown="",
            html=html,
            title=title,
            links=extract_internal_links(html, base_url=url),
            capture_method="browser_fallback",
        )

    def _is_useful_capture(self, result: CrawlResult) -> bool:
        if not result.html.strip():
            return False
        text = extract_text_from_html(result.html, fallback_url=result.url)
        lowered = text.lower()
        if len(text.strip()) < 250:
            return False
        if "ask elliott the eagle" in lowered and len(text.strip()) < 500:
            return False
        return True

    def _usable_markdown(self, markdown: str) -> str | None:
        if not markdown.strip():
            return None
        lowered = markdown.lower()
        if "ask elliott the eagle" in lowered:
            return None
        return markdown
