from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page
import re
import json
import logging
from typing import Set, Optional

logger = logging.getLogger("extractor")

FILE_PATTERN = re.compile(r"https://www\.patreon\.com/file\?h=\d+&m=\d+")


class PatreonScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.pw = None
        self.browser: Optional[Browser] = None
        self.ctx: Optional[BrowserContext] = None

    def __enter__(self):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.headless)
        self.ctx = self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.browser:
            self.browser.close()
        if self.pw:
            self.pw.stop()

    def get_links_from_post(
        self, url: str, wait_time: float = 8.0
    ) -> tuple[Set[str], Optional[str]]:
        links: Set[str] = set()
        creator: Optional[str] = None
        logger.info("[POST] %s", url)

        if not self.ctx:
            raise RuntimeError("Scraper not initialized")

        page = self.ctx.new_page()

        def handle_network(response):
            try:
                ctype = response.headers.get("content-type") or ""
                if "application/json" in ctype:
                    data = response.json()
                    content = json.dumps(data)
                elif "text/html" in ctype:
                    content = response.text()
                else:
                    return
            except Exception:
                return

            found = FILE_PATTERN.findall(content)
            if found:
                prev_len = len(links)
                links.update(found)
                if len(links) > prev_len:
                    logger.info(
                        "[+] captured %d new file link(s) from network",
                        len(links) - prev_len,
                    )

        page.on("response", handle_network)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(wait_time * 1100)

            try:
                selectors = [
                    'a[href*="patreon.com/"] h3',
                    '[data-tag="creator-name"]',
                    'h1[data-tag="creator-name"]',
                    'a[data-tag="creator-name"]',
                    ".cm-cyndlL",
                ]

                for sel in selectors:
                    nodes = page.locator(sel).all()
                    for node in nodes:
                        try:
                            if node.is_visible():
                                val = node.inner_text().strip()
                                if val and val not in [
                                    "Posts",
                                    "About",
                                    "Collections",
                                    "Shop",
                                    "New",
                                ]:
                                    creator = val
                                    break
                        except:
                            continue
                    if creator:
                        break

                if not creator:
                    items = page.eval_on_selector_all(
                        "a:has(h3)",
                        "nodes => nodes.map(n => ({href: n.href, text: n.querySelector('h3').innerText}))",
                    )
                    for item in items:
                        h = item["href"]
                        t = item["text"].strip()
                        if "/posts/" not in h and "/file?" not in h and t:
                            creator = t
                            break

            except Exception as err:
                logger.debug("Failed to get creator: %s", err)

            all_hrefs = page.eval_on_selector_all(
                "a[href]", "elements => elements.map(e => e.href)"
            )
            valid_hrefs = [h for h in all_hrefs if FILE_PATTERN.search(h)]
            if valid_hrefs:
                prev = len(links)
                links.update(valid_hrefs)
                if len(links) > prev:
                    logger.info(
                        "[+] captured %d new file link(s) from DOM",
                        len(links) - prev,
                    )

            full_content = page.content()
            matches = FILE_PATTERN.findall(full_content)
            if matches:
                curr_len = len(links)
                links.update(matches)
                if len(links) > curr_len:
                    logger.info(
                        "[+] captured %d new file link(s) from page",
                        len(links) - curr_len,
                    )

        except Exception as e:
            logger.error("Error: %s", e)
        finally:
            page.close()

        logger.info(
            "[✓] total unique files found: %d | Creator: %s",
            len(links),
            creator or "Unknown",
        )
        return links, creator


def extract_file_links(post_url: str) -> tuple[Set[str], Optional[str]]:
    with PatreonScraper() as scraper:
        return scraper.get_links_from_post(post_url)
