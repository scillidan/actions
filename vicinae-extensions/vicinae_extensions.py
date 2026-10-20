#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Track new extensions on the Vicinae store -> append-only txt log + RSS 2.0.

Data source: https://api.vicinae.com/v1/store/list (public JSON, no auth;
the same API the site itself uses). Dedup key: extension id.
"""

import argparse
import datetime as dt
import email.utils
import json
import os
import sys
import time
from xml.sax.saxutils import escape as xesc

import httpx

API = "https://api.vicinae.com/v1/store/list"
SITE = "https://www.vicinae.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)["extensions"]
    except FileNotFoundError:
        return {}


def save_state(path, extensions):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(
            {"version": 1, "extensions": extensions},
            f,
            ensure_ascii=False,
            indent=1,
        )


def fetch_page(client, page, retries=3):
    for attempt in range(retries):
        try:
            r = client.get(API, params={"page": page, "limit": 500})
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code}: store list p{page}", file=sys.stderr)
        except Exception as e:
            print(f"  error ({e}): store list p{page}", file=sys.stderr)
        if attempt < retries - 1:
            time.sleep(2 * (attempt + 1))
    return None


def fetch_all(client):
    extensions, page = [], 1
    while True:
        data = fetch_page(client, page)
        if data is None:
            return None
        extensions.extend(data["extensions"])
        if not data["pagination"]["hasNext"]:
            return extensions
        page += 1


def append_txt(path, new_entries):
    if not new_entries:
        return
    lines = [
        f"{e['date']} {e['url']} {e['title']} - {e['author']}" for e in new_entries
    ]
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def build_rss(entries, path, limit, feed_title):
    items = sorted(entries.values(), key=lambda e: (e["date"], e["url"]), reverse=True)[
        :limit
    ]
    now = email.utils.format_datetime(dt.datetime.now(dt.timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0">',
        "<channel>",
        f"<title>{xesc(feed_title)}</title>",
        f"<link>{SITE}/extensions</link>",
        f"<description>{xesc(feed_title)}</description>",
        "<language>en</language>",
        f"<lastBuildDate>{now}</lastBuildDate>",
    ]
    for e in items:
        d = dt.datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        cats = "".join(f"<category>{xesc(c)}</category>" for c in e["categories"])
        parts += [
            "<item>",
            f"<title>{xesc(e['title'])}</title>",
            f"<link>{e['url']}</link>",
            f'<guid isPermaLink="true">{e["url"]}</guid>',
            f"<pubDate>{email.utils.format_datetime(d)}</pubDate>",
            f"<description>{xesc(e['description'] + ' — ' + e['author'])}</description>",
            cats,
            "</item>",
        ]
    parts += ["</channel>", "</rss>", ""]
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(parts))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--txt", default="vicinae-updates.txt")
    ap.add_argument("--rss", default="vicinae-feed.xml")
    ap.add_argument("--feed-title", default="Vicinae new extensions")
    ap.add_argument("--limit", type=int, default=100, help="max RSS items")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    state = load_state(args.state)
    with httpx.Client(headers={"User-Agent": UA}, timeout=30) as client:
        extensions = fetch_all(client)
    if extensions is None:
        print("store list unavailable", file=sys.stderr)
        return 1

    new_entries = []
    for ext in extensions:
        if ext["id"] in state:
            continue
        handle = ext["author"]["handle"]
        state[ext["id"]] = {
            "name": ext["name"],
            "title": ext["title"],
            "description": ext["description"] or "",
            "author": ext["author"]["name"] or handle,
            "url": f"{SITE}/extensions/{handle}/{ext['name']}",
            "date": ext["createdAt"],
            "categories": [c["name"] for c in ext["categories"]],
        }
        new_entries.append(state[ext["id"]])

    new_entries.sort(key=lambda e: (e["date"], e["url"]), reverse=True)
    if not new_entries:
        # no writes: rebuilding the feed would only bump lastBuildDate and
        # create an empty commit every run
        print(
            f"+0 new extensions (state: {len(state)}, store total: {len(extensions)})"
        )
        return 0
    append_txt(args.txt, new_entries)
    build_rss(state, args.rss, args.limit, args.feed_title)
    save_state(args.state, state)

    print(
        f"+{len(new_entries)} new extensions "
        f"(state: {len(state)}, store total: {len(extensions)})"
    )
    if "GITHUB_ENV" in os.environ:
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write(f"NEW_EXTENSIONS={len(new_entries)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
