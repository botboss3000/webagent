#!/usr/bin/env python3
"""
News Headline Scraper
---------------------
Scrapes headlines from 5 different RSS feeds and saves them to a CSV file.
Uses feedparser + requests with robust error handling and logging.
"""

import csv
import logging
import sys
from datetime import datetime, timezone
from typing import List, Dict

import feedparser
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# 5 diverse RSS feeds covering different regions/topics
RSS_FEEDS: List[Dict[str, str]] = [
    {
        "name": "BBC Top Stories",
        "url": "https://feeds.bbci.co.uk/news/rss.xml",
    },
    {
        "name": "Reuters Top News",
        "url": "https://www.reutersagency.com/feed/",
    },
    {
        "name": "NPR News",
        "url": "https://feeds.npr.org/1001/rss.xml",
    },
    {
        "name": "CNBC Top News",
        "url": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114",
    },
    {
        "name": "Associated Press (AP) Top News",
        "url": "https://rsshub.app/apnews/topics/apf-topnews",
    },
]

CSV_OUTPUT = "headlines.csv"
REQUEST_TIMEOUT = 15  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),     # console
        logging.FileHandler("news_scraper.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("news_scraper")

# ---------------------------------------------------------------------------
# Feed fetching & parsing
# ---------------------------------------------------------------------------


def fetch_feed(url: str, name: str) -> str | None:
    """Fetch the raw XML of an RSS feed via HTTP GET.

    Returns the response text on success, or *None* on failure.
    """
    try:
        log.info("Fetching feed: %s (%s)", name, url)
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        log.info("  ✓ %s — HTTP %s (%.1f KB)", name, resp.status_code, len(resp.content) / 1024)
        return resp.text
    except requests.exceptions.Timeout:
        log.error("  ✗ %s — request timed out after %s s", name, REQUEST_TIMEOUT)
    except requests.exceptions.HTTPError as exc:
        log.error("  ✗ %s — HTTP error: %s", name, exc)
    except requests.exceptions.ConnectionError as exc:
        log.error("  ✗ %s — connection error: %s", name, exc)
    except requests.exceptions.RequestException as exc:
        log.error("  ✗ %s — request failed: %s", name, exc)
    return None


def parse_feed(xml_text: str, name: str) -> List[Dict[str, str]]:
    """Parse RSS XML into a list of headline dicts.

    Each dict has keys: *source*, *title*, *link*, *published*, *summary*.
    """
    parsed = feedparser.parse(xml_text)
    if parsed.bozo and not parsed.entries:
        log.warning("  ⚠ %s — feed is malformed (bozo bit set), but trying entries anyway", name)

    if not parsed.entries:
        log.warning("  ⚠ %s — no entries found in feed", name)
        return []

    entries = []
    for entry in parsed.entries:
        # feedparser normalises 'published' / 'updated' into 'published_parsed'
        published = ""
        if hasattr(entry, "published"):
            published = entry.published
        elif hasattr(entry, "updated"):
            published = entry.updated

        summary = ""
        if hasattr(entry, "summary"):
            summary = entry.summary
        elif hasattr(entry, "description"):
            summary = entry.description

        entries.append({
            "source": name,
            "title": entry.get("title", "").strip(),
            "link": entry.get("link", "").strip(),
            "published": published,
            "summary": summary.strip(),
        })

    log.info("  → %s — %s headlines parsed", name, len(entries))
    return entries


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

HEADER_FIELDS = ["source", "title", "link", "published", "summary"]


def save_to_csv(headlines: List[Dict[str, str]], path: str) -> None:
    """Write the list of headline dicts to a CSV file."""
    if not headlines:
        log.warning("No headlines to write — CSV will be empty.")
        return

    try:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=HEADER_FIELDS)
            writer.writeheader()
            writer.writerows(headlines)
        log.info("Wrote %s headlines to %s", len(headlines), path)
    except OSError as exc:
        log.critical("Failed to write CSV at %s — %s", path, exc)
        raise


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("=" * 58)
    log.info("  News Headline Scraper — started at %s", datetime.now(timezone.utc).isoformat())
    log.info("=" * 58)

    all_headlines: List[Dict[str, str]] = []

    for feed in RSS_FEEDS:
        name = feed["name"]
        url = feed["url"]

        xml_text = fetch_feed(url, name)
        if xml_text is None:
            continue  # already logged

        try:
            entries = parse_feed(xml_text, name)
        except Exception as exc:
            log.error("  ✗ %s — parse error: %s", name, exc, exc_info=True)
            continue

        all_headlines.extend(entries)

    # Deduplicate by (title, source) in case of overlapping feeds
    seen = set()
    unique: List[Dict[str, str]] = []
    for h in all_headlines:
        key = (h["title"].lower(), h["source"])
        if key not in seen:
            seen.add(key)
            unique.append(h)

    if len(unique) < len(all_headlines):
        log.info("Removed %s duplicate(s)", len(all_headlines) - len(unique))

    log.info("Total unique headlines scraped: %s", len(unique))

    save_to_csv(unique, CSV_OUTPUT)

    # Quick summary
    source_counts: Dict[str, int] = {}
    for h in unique:
        source_counts[h["source"]] = source_counts.get(h["source"], 0) + 1

    log.info("-" * 58)
    log.info("  Breakdown by source:")
    for src, count in sorted(source_counts.items(), key=lambda x: -x[1]):
        log.info("    %-30s %s", src, count)
    log.info("-" * 58)
    log.info("Done — output: %s", CSV_OUTPUT)


if __name__ == "__main__":
    main()