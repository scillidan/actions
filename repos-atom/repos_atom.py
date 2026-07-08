#!/usr/bin/env python3
import asyncio
import calendar
import datetime
import os
from urllib.parse import urlparse

import aiohttp
import feedparser

INPUT_FILE = "repos.txt"

FEED_TYPES = ("releases", "tags", "commits")
OUTPUT_MAP = {ft: f"repos-{ft}.txt" for ft in FEED_TYPES}


def normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    return parsed._replace(fragment="", query="").geturl().rstrip("/")


def st_to_datetime(t) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(calendar.timegm(t), tz=datetime.timezone.utc)


def load_processed(output_file: str) -> set:
    try:
        with open(output_file, "r", encoding="utf-8") as f:
            return {normalize_url(line.split()[1]) for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def parse_repos_txt(path: str) -> dict:
    result = {ft: [] for ft in FEED_TYPES}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if " - " in stripped:
                repo_url, feed_type = stripped.rsplit(" - ", 1)
                feed_type = feed_type.strip().lower()
                if feed_type not in FEED_TYPES:
                    print(f"Unknown feed type '{feed_type}', skipping: {stripped}")
                    continue
                result[feed_type].append(repo_url.strip())
            else:
                for ft in FEED_TYPES:
                    result[ft].append(stripped)
    return result


async def fetch(session, url):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status == 200:
                return feedparser.parse(await resp.text())
            print(f"HTTP {resp.status}: {url}")
    except Exception as e:
        print(f"Error: {url} ({e})")
    return None


def atom_url(repo_url: str, feed_type: str) -> str:
    suffix = (
        "releases.atom"
        if feed_type == "releases"
        else ("tags.atom" if feed_type == "tags" else "commits.atom")
    )
    return repo_url.rstrip("/") + "/" + suffix


async def process_feed_type(session, feed_type, repo_urls):
    processed = load_processed(OUTPUT_MAP[feed_type])
    urls = [atom_url(r, feed_type) for r in repo_urls]
    feeds = await asyncio.gather(*[fetch(session, u) for u in urls])

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
            dt = (
                st_to_datetime(dt_struct)
                if dt_struct
                else (datetime.datetime.now(datetime.timezone.utc))
            )
            new_entries.append((dt, norm))

    new_entries.sort(reverse=True)

    if new_entries:
        with open(OUTPUT_MAP[feed_type], "a", encoding="utf-8") as f:
            for dt, norm in new_entries:
                f.write(f"{dt.strftime('%Y-%m-%dT%H:%M:%S')} {norm}\n")

    print(
        f"[{feed_type}] +{len(new_entries)} new entries (total: {len(processed) + len(new_entries)})"
    )
    return len(new_entries)


async def main():
    repos_by_type = parse_repos_txt(INPUT_FILE)

    async with aiohttp.ClientSession() as session:
        totals = await asyncio.gather(
            *[
                process_feed_type(session, ft, repos_by_type[ft])
                for ft in FEED_TYPES
                if repos_by_type[ft]
            ]
        )

    total_new = sum(totals)
    print(f"Total: +{total_new} new entries across all feed types")

    if "GITHUB_ENV" in os.environ:
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"NEW_ENTRIES={total_new}\n")


if __name__ == "__main__":
    asyncio.run(main())
