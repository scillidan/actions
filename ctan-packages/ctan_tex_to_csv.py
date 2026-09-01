#!/usr/bin/env python3
"""Generate ctan-packages.csv from ctan.txt.

Input:
  - ctan.txt: one CTAN URL per line (https://www.ctan.org/pkg/<pkg>)
Output:
  - ctan-packages.csv: package name, title, topics (official CTAN casing)

The script fetches each package's caption and topics from CTAN's public JSON
API, then maps topic machine IDs to the human-readable display names used on
ctan.org.
"""

import argparse
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

CTAN_PKG_API = "https://www.ctan.org/json/2.0/pkg/{pkg}"
CTAN_TOPICS_API = "https://www.ctan.org/json/2.0/topics"
CTAN_TOPIC_LIST_URL = "https://www.ctan.org/topics/highscore"


def fetch_json(url: str, retries: int = 3):
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "ctan-csv-bot/1.0"},
    )
    last_exc = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429 and i < retries - 1:
                time.sleep(2**i)
                continue
            raise
    raise last_exc or Exception(f"Failed to fetch {url}")


def fetch_text(url: str, retries: int = 3):
    req = urllib.request.Request(
        url,
        headers={"Accept": "text/html", "User-Agent": "ctan-csv-bot/1.0"},
    )
    last_exc = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429 and i < retries - 1:
                time.sleep(2**i)
                continue
            raise
    raise last_exc or Exception(f"Failed to fetch {url}")


def strip_tags(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text)


def get_topic_display_mapping() -> dict[str, str]:
    """Scrape the human-readable topic names from CTAN's topic list page."""
    html = fetch_text(CTAN_TOPIC_LIST_URL)
    # The high-score page contains rows like:
    #   <tr ... tag="key"> <td ... title="...">Display Name</td> ... </tr>
    # Some title attributes contain malformed HTML with extra quotes, so we
    # isolate the <tr>, take the first <td> cell, and use the text after the
    # last "> marker as the display name.
    tr_pattern = re.compile(r'<tr[^>]*tag="([^"]+)"[^>]*>(.*?)</tr>', re.S)
    td_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
    mapping: dict[str, str] = {}
    for key, tr_content in tr_pattern.findall(html):
        td_match = td_pattern.search(tr_content)
        if not td_match:
            continue
        cell = td_match.group(1)
        # Heuristic: if the cell looks polluted by a malformed title attribute,
        # discard everything up to and including the final "> sequence.
        if (
            '">Tagged' in cell
            or '">PGF' in cell
            or '">Font' in cell
            or '">Dummy' in cell
            or "tagging-status" in cell
        ):
            idx = cell.rfind('">')
            if idx != -1:
                cell = cell[idx + 2 :]
        display = strip_tags(cell).strip()
        if key and display:
            mapping[key] = display
    return mapping


def get_topic_details_mapping() -> dict[str, str]:
    """Get topic key -> details from the CTAN JSON API as a fallback."""
    data = fetch_json(CTAN_TOPICS_API)
    return {item["key"]: item["details"] for item in data}


def pkg_from_url(url: str) -> str:
    # CTAN URLs may contain LaTeX-escaped underscores from old .tex sources.
    url = url.replace("\\_", "_")
    match = re.search(r"/pkg/([^/]+)/?$", url)
    if not match:
        raise ValueError(f"Cannot extract package name from URL: {url}")
    return match.group(1)


def fetch_pkg_info(pkg: str) -> dict | None:
    """Return package metadata from CTAN, or None if the package is unknown.

    CTAN's canonical package names are lowercase in the JSON API, so if the
    original casing returns 404 we try once with the lowercase name.
    """
    candidates = [pkg]
    if pkg != pkg.lower():
        candidates.append(pkg.lower())

    for candidate in candidates:
        try:
            data = fetch_json(CTAN_PKG_API.format(pkg=candidate))
            return {
                "pkg": candidate,
                "title": data.get("caption", ""),
                "topics": data.get("topics", []),
            }
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue
            raise
    return None


def format_topic(
    tid: str, display_map: dict[str, str], details_map: dict[str, str]
) -> str:
    if tid in display_map:
        return display_map[tid]
    details = details_map.get(tid, tid)
    if details:
        return details[0].upper() + details[1:]
    return tid


def fetch_packages(pkgs: list[str], max_workers: int = 16) -> dict[str, dict | None]:
    """Fetch CTAN metadata for many packages concurrently."""
    results: dict[str, dict | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_pkg = {executor.submit(fetch_pkg_info, pkg): pkg for pkg in pkgs}
        for future in as_completed(future_to_pkg):
            pkg = future_to_pkg[future]
            try:
                results[pkg] = future.result()
            except Exception as e:
                print(
                    f"WARNING: failed to fetch metadata for {pkg}: {e}", file=sys.stderr
                )
                results[pkg] = None
    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate ctan.csv from ctan.txt by fetching CTAN metadata."
    )
    parser.add_argument(
        "--txt",
        default="ctan.txt",
        help="Input URL list (default: ctan.txt)",
    )
    parser.add_argument(
        "--csv",
        default="ctan-packages.csv",
        help="Output CSV filename (default: ctan-packages.csv)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Concurrency for CTAN API requests (default: 16)",
    )
    args = parser.parse_args()

    txt_path = Path(args.txt)
    if not txt_path.is_file():
        print(f"ERROR: input file not found: {txt_path}", file=sys.stderr)
        return 1

    urls = [
        line.strip()
        for line in txt_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        print("ERROR: no URLs found in input file", file=sys.stderr)
        return 1

    pkgs = []
    for url in urls:
        try:
            pkgs.append(pkg_from_url(url))
        except ValueError as e:
            print(f"WARNING: {e}", file=sys.stderr)

    if not pkgs:
        print("ERROR: no valid CTAN package URLs found", file=sys.stderr)
        return 1

    print("Fetching CTAN topic display names...", file=sys.stderr)
    display_map = get_topic_display_mapping()
    details_map = get_topic_details_mapping()

    print(f"Fetching metadata for {len(pkgs)} packages...", file=sys.stderr)
    pkg_infos = fetch_packages(pkgs, max_workers=args.max_workers)

    rows = []
    for pkg in pkgs:
        info = pkg_infos.get(pkg)
        if info is None:
            print(f"WARNING: package not found on CTAN: {pkg}", file=sys.stderr)
            continue
        topics = [format_topic(tid, display_map, details_map) for tid in info["topics"]]
        rows.append(
            {
                "pkg": pkg,
                "title": info["title"],
                "topics": ", ".join(topics),
            }
        )

    if not rows:
        print("ERROR: no packages could be processed", file=sys.stderr)
        return 1

    csv_path = Path(args.csv)
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["pkg", "title", "topics"])
        for r in rows:
            writer.writerow([r["pkg"], r["title"], r["topics"]])

    print(f"Done: {csv_path} ({len(rows)} packages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
