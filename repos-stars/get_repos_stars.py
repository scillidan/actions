#!/usr/bin/env python3

# Authors: Hy3-preview🧙‍♂️, scillidan🤡

import requests
import csv
import time
import os
from typing import Optional

INPUT_FILE = "repos.txt"
OUTPUT_FILE = "repos-stars.csv"

def simplify_stars(count: int) -> str:
    if count >= 1_000:
        return f">{count // 1000}k"
    return str(count)

def fetch_stars(repo_url: str, token: Optional[str] = None) -> int:
    api_url = repo_url.replace(
        "https://github.com/", "https://api.github.com/repos/"
    )
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"

    try:
        r = requests.get(api_url, headers=headers, timeout=10)
        r.raise_for_status()
        return r.json().get("stargazers_count", 0)
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 403:
            print("⚠ Rate limit exceeded, sleeping 60s...")
            time.sleep(60)
            return fetch_stars(repo_url, token)
        print(f"✗ Failed: {repo_url} ({e})")
        return 0
    except Exception as e:
        print(f"✗ Error: {repo_url} ({e})")
        return 0

def main():

    token = os.environ.get("GH_TOKEN")

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        repos = [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]

    results = []
    for i, repo in enumerate(repos, start=1):
        print(f"[{i}/{len(repos)}] {repo}")
        stars = fetch_stars(repo, token)
        results.append((repo, simplify_stars(stars)))
        time.sleep(0.5)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["repo", "stars"])
        writer.writerows(results)

    print(f"\n✅ Generated {OUTPUT_FILE}")

if __name__ == "__main__":
    main()