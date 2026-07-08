#!/usr/bin/env python3
# Based on https://github.com/hitem/rss-aggregator/blob/main/rss_aggregator.py
# Retained: normalize_url, struct_time_to_datetime, async feed fetch+dedup,
#            processed-links append pattern, GITHUB_ENV output.
# Removed:  XML feed generation, BeautifulSoup/lxml, git reset, time threshold filter.

import asyncio
import calendar
import datetime
import os
from urllib.parse import urlparse

import aiohttp
import feedparser

INPUT_FILE = "repos.txt"
OUTPUT_FILE = "repos-releases.txt"


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def st_to_datetime(t) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(calendar.timegm(t), tz=datetime.timezone.utc)


def load_processed() -> set:
    try:
        with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
            return {normalize_url(line.split()[1]) for line in f if line.strip()}
    except FileNotFoundError:
        return set()


async def fetch(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return feedparser.parse(await resp.text())
            print(f"HTTP {resp.status}: {url}")
    except Exception as e:
        print(f"Error: {url} ({e})")
    return None


async def main():
    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        repos = [
            line.strip() for line in f if line.strip() and not line.startswith("#")
        ]

    atom_urls = [r.rstrip("/") + "/releases.atom" for r in repos]
    processed = load_processed()

    async with aiohttp.ClientSession() as session:
        feeds = await asyncio.gather(*[fetch(session, u) for u in atom_urls])

    new_entries = []
    seen = set()
    for feed in feeds:
        if not feed or not feed.entries:
            continue
        for entry in feed.entries:
            if not hasattr(entry, "link"):
                continue
            norm = normalize_url(entry.link)
            if norm in processed or norm in seen:
                continue
            seen.add(norm)
            dt_struct = getattr(entry, "published_parsed", None) or getattr(
                entry, "updated_parsed", None
            )
            if dt_struct:
                dt = st_to_datetime(dt_struct)
            else:
                dt = datetime.datetime.now(datetime.timezone.utc)
            new_entries.append(
                (dt, norm, entry.title if hasattr(entry, "title") else "")
            )

    new_entries.sort(reverse=True)

    with open(OUTPUT_FILE, "a", encoding="utf-8") as f:
        for dt, norm, title in new_entries:
            f.write(f"{dt.strftime('%Y-%m-%dT%H:%M:%S')} {norm}\n")

    print(
        f"+{len(new_entries)} new releases tracked (total processed: {len(processed) + len(new_entries)})"
    )

    if "GITHUB_ENV" in os.environ:
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"NEW_RELEASES={len(new_entries)}\n")


if __name__ == "__main__":
    asyncio.run(main())
