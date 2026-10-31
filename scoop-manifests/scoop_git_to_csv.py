#!/usr/bin/env python3
"""Generate Scoop manifest status CSVs from buckets.txt.

Input:
  - buckets.txt: one scoop bucket git repo URL per line

Output:
  - added_<bucket>.csv: active manifests not in any status list below
                        (name,added_date,description)
  - archived.csv:       GitHub repositories archived by their owner
                        (bucket,name,archived_date)
  - broken_home.csv:    homepages confirmed broken
                        (bucket,name,broken_home)
  - inactive.csv:       manifests unchanged for more than five years
                        (bucket,name,latest_update)
  - deprecated.csv:     manifests currently in deprecated/
                        (bucket,name,deprecated_date,description)
  - scoop-manifests.meta.json: run metadata and per-bucket counts

Dates are commit dates (YYYY-MM-DD), except archived_date which comes from
GitHub. Rows are sorted newest first. Each bucket is kept as a cached bare
clone (.cache/<bucket>.git): one clone on first run, then one incremental
fetch per run. A failed fetch falls back to the cached clone.

GitHub archive checks use GraphQL in small batches and require GITHUB_TOKEN or
GH_TOKEN. The repository comes from homepage, falling back to checkver.github.
Successful API and homepage results are cached in .cache/github-status.json for
7 days, so weekly runs only query new or expired entries. Homepage checks are
sequential and delayed to stay upstream-friendly.

Use --offline to skip all fetching and HTTP checks. Existing archived.csv and
broken_home.csv are reused in that mode; buckets without a cached clone are
skipped with a warning.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import tarfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = Path(__file__).resolve().parent
API_CACHE_TTL_DAYS = 7
GITHUB_BATCH_SIZE = 10
GITHUB_BATCH_DELAY_SECONDS = 0.5
HOMEPAGE_DELAY_SECONDS = 0.2


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout


def bucket_name(url: str) -> str:
    base = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in base).lower()


def ensure_repo(url: str, cache: Path, offline: bool) -> None:
    """Create the cached bare clone, or incrementally fetch the default branch."""
    if (cache / "HEAD").exists():
        if offline:
            return
        branch = git(cache, "symbolic-ref", "HEAD").strip().removeprefix("refs/heads/")
        try:
            git(
                cache,
                "fetch",
                "--quiet",
                "origin",
                f"+refs/heads/{branch}:refs/heads/{branch}",
            )
        except subprocess.CalledProcessError as e:
            msg = e.stderr.strip() if e.stderr else str(e)
            print(
                f"WARNING: fetch failed for {url} ({msg}); using cached clone",
                file=sys.stderr,
            )
        return
    if offline:
        raise RuntimeError(f"no cached clone at {cache}")
    cache.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--bare", "--single-branch", "--quiet", url, str(cache)],
        check=True,
        capture_output=True,
        text=True,
    )


def first_add_dates(repo: Path, prefix: str) -> dict[str, str]:
    """manifest name (relative to prefix, no .json) -> first appearance date.

    Log output is newest-first, so always overwriting leaves the earliest date.
    --no-renames makes moves into the directory show up as additions.
    """
    out = git(
        repo,
        "log",
        "--no-renames",
        "--diff-filter=A",
        "--format=@@%cs",
        "--name-only",
        "HEAD",
        "--",
        f"{prefix}/",
    )
    dates: dict[str, str] = {}
    cur = ""
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:]
        elif line:
            rel = line.removeprefix(f"{prefix}/")
            if rel.lower().endswith(".json"):
                dates[rel[:-5]] = cur
    return dates


def last_update_dates(repo: Path, prefix: str) -> dict[str, str]:
    """manifest name -> last commit date; log output is newest-first."""
    out = git(
        repo,
        "log",
        "--no-renames",
        "--format=@@%cs",
        "--name-only",
        "HEAD",
        "--",
        f"{prefix}/",
    )
    dates: dict[str, str] = {}
    cur = ""
    for line in out.splitlines():
        if line.startswith("@@"):
            cur = line[2:]
        elif line:
            rel = line.removeprefix(f"{prefix}/")
            if rel.lower().endswith(".json"):
                dates.setdefault(rel[:-5], cur)
    return dates


def current_manifests(repo: Path, prefix: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", f"{prefix}/")
    return [
        line.removeprefix(f"{prefix}/")[:-5]
        for line in out.splitlines()
        if line.lower().endswith(".json")
    ]


def manifest_info(repo: Path, prefix: str) -> dict[str, dict[str, str]]:
    """manifest name -> description, homepage, and checkver.github."""
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD", "--", f"{prefix}/"],
        stdout=subprocess.PIPE,
    )
    info: dict[str, dict[str, str]] = {}
    assert proc.stdout is not None
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name.removeprefix("./").removeprefix(f"{prefix}/")
            if not name.lower().endswith(".json"):
                continue
            fobj = tar.extractfile(member)
            if fobj is None:
                continue
            try:
                data = json.loads(fobj.read().decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                continue
            checkver = data.get("checkver")
            checkver_github = (
                checkver.get("github", "") if isinstance(checkver, dict) else ""
            )
            values = {
                "description": data.get("description", ""),
                "homepage": data.get("homepage", ""),
                "github": checkver_github,
            }
            info[name[:-5]] = {
                key: value if isinstance(value, str) else ""
                for key, value in values.items()
            }
    proc.wait()
    return info


def write_csv(
    path: Path, header: list[str], rows: list[tuple], date_column: int = 1
) -> None:
    rows = sorted(rows)
    rows.sort(key=lambda r: r[date_column], reverse=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def read_csv_rows(path: Path) -> list[tuple]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        next(reader, None)
        return [tuple(row) for row in reader]


def github_repo(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.hostname not in (
        "github.com",
        "www.github.com",
    ):
        return None
    path = [part for part in parts.path.split("/") if part]
    if len(path) < 2:
        return None
    return path[0], path[1].removesuffix(".git")


def load_api_cache(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "archived": data.get("archived")
        if isinstance(data.get("archived"), dict)
        else {},
        "homepage": data.get("homepage")
        if isinstance(data.get("homepage"), dict)
        else {},
    }


def save_api_cache(path: Path, cache: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def cache_entry_fresh(entry: object, today: date) -> bool:
    if not isinstance(entry, dict):
        return False
    try:
        checked = date.fromisoformat(str(entry.get("checked_at", "")))
    except ValueError:
        return False
    return 0 <= (today - checked).days <= API_CACHE_TTL_DAYS


def github_archived_dates(
    repos: set[tuple[str, str]],
    cache_path: Path,
    cache: dict[str, dict],
    today: date,
) -> dict[tuple[str, str], str | None]:
    """Return archived date, empty string for active, or None for missing repos."""
    statuses: dict[tuple[str, str], str | None] = {}
    pending: list[tuple[str, str]] = []
    cached = cache["archived"]
    for repo in sorted(repos):
        key = "/".join(repo)
        entry = cached.get(key)
        status = entry.get("archived_date") if isinstance(entry, dict) else None
        if cache_entry_fresh(entry, today) and (
            status is None or isinstance(status, str)
        ):
            statuses[repo] = status
        else:
            pending.append(repo)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if pending and not token:
        raise RuntimeError(
            "GITHUB_TOKEN or GH_TOKEN is required for GitHub archive checks"
        )

    for start in range(0, len(pending), GITHUB_BATCH_SIZE):
        batch = pending[start : start + GITHUB_BATCH_SIZE]
        fields = " ".join(
            f"r{i}: repository(owner: {json.dumps(owner)}, name: {json.dumps(repo)}) "
            "{ isArchived archivedAt }"
            for i, (owner, repo) in enumerate(batch)
        )
        request = Request(
            "https://api.github.com/graphql",
            data=json.dumps({"query": f"query {{ {fields} }}"}).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "scoop-manifests-tracker",
            },
        )
        for attempt in range(4):
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("GraphQL response is not an object")
                errors = payload.get("errors")
                rate_limited = isinstance(errors, list) and any(
                    "rate limit" in str(error.get("message", "")).lower()
                    for error in errors
                    if isinstance(error, dict)
                )
                if rate_limited:
                    if attempt == 3:
                        raise RuntimeError("GitHub GraphQL request failed: rate limit")
                    time.sleep(60)
                    continue
                break
            except HTTPError as e:
                rate_limited = e.code == 429 or (
                    e.code == 403
                    and (
                        e.headers.get("Retry-After") is not None
                        or e.headers.get("X-RateLimit-Remaining") == "0"
                    )
                )
                if not (rate_limited or e.code in (500, 502, 503, 504)) or attempt == 3:
                    raise RuntimeError(
                        f"GitHub GraphQL request failed: HTTP {e.code}"
                    ) from e
                delay = 2**attempt
                if e.code == 429:
                    try:
                        delay = max(delay, int(e.headers.get("Retry-After", "0")))
                    except ValueError:
                        pass
                if e.headers.get("X-RateLimit-Remaining") == "0":
                    try:
                        reset = int(e.headers.get("X-RateLimit-Reset", "0"))
                        delay = max(delay, max(0, reset - int(time.time()) + 1))
                    except ValueError:
                        pass
            except (URLError, TimeoutError, ValueError) as e:
                if attempt == 3:
                    raise RuntimeError(f"GitHub GraphQL request failed: {e}") from e
                delay = 2**attempt
            time.sleep(delay)

        data = payload.get("data")
        if not isinstance(data, dict):
            raise RuntimeError("GitHub GraphQL returned no repository data")
        batch_statuses: dict[tuple[str, str], str | None] = {}
        for i, repo in enumerate(batch):
            alias = f"r{i}"
            if alias not in data:
                raise RuntimeError("GitHub GraphQL returned incomplete repository data")
            node = data[alias]
            if node is None:
                batch_statuses[repo] = None
            elif node.get("isArchived"):
                archived_at = node.get("archivedAt")
                if not isinstance(archived_at, str):
                    raise RuntimeError(
                        f"Archived GitHub repository {repo} has no archivedAt"
                    )
                batch_statuses[repo] = archived_at[:10]
            else:
                batch_statuses[repo] = ""
        statuses.update(batch_statuses)
        checked_at = today.isoformat()
        for repo, status in batch_statuses.items():
            cached["/".join(repo)] = {
                "archived_date": status,
                "checked_at": checked_at,
            }
        save_api_cache(cache_path, cache)
        if start + GITHUB_BATCH_SIZE < len(pending):
            time.sleep(GITHUB_BATCH_DELAY_SECONDS)
    return statuses


def check_homepage(url: str, cache: dict[str, dict], today: date) -> bool:
    cached = cache["homepage"]
    entry = cached.get(url)
    if cache_entry_fresh(entry, today) and isinstance(entry.get("broken"), bool):
        return entry["broken"]
    time.sleep(HOMEPAGE_DELAY_SECONDS)
    broken = homepage_broken(url)
    cached[url] = {"broken": broken, "checked_at": today.isoformat()}
    return broken


def homepage_broken(url: str) -> bool:
    try:
        if urlsplit(url).scheme not in ("http", "https"):
            return False
    except ValueError:
        return True

    for method in ("HEAD", "GET"):
        for attempt in range(3):
            request = Request(
                url,
                method=method,
                headers={"User-Agent": "scoop-manifests-tracker"},
            )
            try:
                with urlopen(request, timeout=20):
                    return False
            except HTTPError as e:
                if method == "HEAD":
                    break
                if e.code in (404, 410):
                    return True
                if e.code not in (429, 500, 502, 503, 504):
                    return False
            except (URLError, TimeoutError, ValueError):
                if method == "HEAD":
                    break
            if attempt < 2:
                time.sleep(attempt + 1)
    return True


def process_bucket(
    url: str,
    cache: Path,
    outdir: Path,
    offline: bool,
    cutoff: str,
    combined: dict[str, list[tuple]],
    api_cache_path: Path,
    api_cache: dict[str, dict],
    today: date,
) -> dict:
    name = bucket_name(url)
    ensure_repo(url, cache, offline)

    add_dates = first_add_dates(cache, "bucket")
    dep_dates = first_add_dates(cache, "deprecated")
    update_dates = last_update_dates(cache, "bucket")
    added = current_manifests(cache, "bucket")
    deprecated = current_manifests(cache, "deprecated")
    added_info = manifest_info(cache, "bucket")
    dep_info = manifest_info(cache, "deprecated")

    root_date = None

    def date_of(dates: dict[str, str], key: str) -> str:
        nonlocal root_date
        d = dates.get(key)
        if not d:
            if root_date is None:
                roots = git(cache, "rev-list", "--max-parents=0", "HEAD").split()
                root_date = min(
                    git(cache, "show", "-s", "--format=%cs", r).strip() for r in roots
                )
            d = root_date
        return d

    combined["deprecated"].extend(
        (
            name,
            manifest,
            date_of(dep_dates, manifest),
            dep_info.get(manifest, {}).get("description", ""),
        )
        for manifest in deprecated
    )

    repos = set()
    for manifest in added:
        info = added_info.get(manifest, {})
        repo = github_repo(info.get("homepage", "")) or github_repo(
            info.get("github", "")
        )
        if repo:
            repos.add(repo)
    github_status = (
        {}
        if offline
        else github_archived_dates(repos, api_cache_path, api_cache, today)
    )

    counts = {"archived": 0, "broken_home": 0, "inactive": 0}
    for manifest in added:
        info = added_info.get(manifest, {})
        latest_update = date_of(update_dates, manifest)
        if latest_update < cutoff:
            combined["inactive"].append((name, manifest, latest_update))
            counts["inactive"] += 1

        if offline:
            continue
        homepage = info.get("homepage", "")
        homepage_repo = github_repo(homepage)
        upstream_repo = homepage_repo or github_repo(info.get("github", ""))
        archived_date = github_status.get(upstream_repo) if upstream_repo else None
        if archived_date:
            combined["archived"].append((name, manifest, archived_date))
            counts["archived"] += 1

        github_home_checked = (
            homepage_repo and github_status.get(homepage_repo) is not None
        )
        if (
            not github_home_checked
            and homepage
            and check_homepage(homepage, api_cache, today)
        ):
            combined["broken_home"].append((name, manifest, "broken_home"))
            counts["broken_home"] += 1

    if not offline:
        save_api_cache(api_cache_path, api_cache)

    excluded = {
        (bucket, manifest)
        for rows in combined.values()
        for bucket, manifest, *_ in rows
    }
    added_rows = [
        (
            manifest,
            date_of(add_dates, manifest),
            added_info.get(manifest, {}).get("description", ""),
        )
        for manifest in added
        if (name, manifest) not in excluded
    ]
    write_csv(
        outdir / f"added_{name}.csv",
        ["name", "added_date", "description"],
        added_rows,
    )

    return {
        "url": url,
        "added": len(added_rows),
        "deprecated": len(deprecated),
        **counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate scoop bucket manifest CSVs from buckets.txt."
    )
    parser.add_argument(
        "--buckets", default=str(BASE / "buckets.txt"), help="bucket URL list"
    )
    parser.add_argument("--outdir", default=str(BASE), help="output directory for CSVs")
    parser.add_argument(
        "--cachedir", default=str(BASE / ".cache"), help="bare clone cache directory"
    )
    parser.add_argument(
        "--offline", action="store_true", help="skip git and HTTP fetching"
    )
    args = parser.parse_args()

    urls = [
        line.strip()
        for line in Path(args.buckets).read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not urls:
        print("ERROR: no bucket URLs found in input file", file=sys.stderr)
        return 1

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    cachedir = Path(args.cachedir)
    api_cache_path = cachedir / "github-status.json"
    api_cache = load_api_cache(api_cache_path)
    combined: dict[str, list[tuple]] = {
        "archived": [],
        "broken_home": [],
        "inactive": [],
        "deprecated": [],
    }
    if args.offline:
        for key in ("archived", "broken_home"):
            combined[key] = read_csv_rows(outdir / f"{key}.csv")

    now = datetime.now(timezone.utc).date()
    try:
        cutoff = now.replace(year=now.year - 5).isoformat()
    except ValueError:  # February 29
        cutoff = now.replace(year=now.year - 5, day=28).isoformat()

    meta = {"generated": "", "buckets": {}}
    failures = 0
    for url in urls:
        name = bucket_name(url)
        try:
            print(f"[{name}] processing {url} ...", file=sys.stderr)
            meta["buckets"][name] = process_bucket(
                url,
                cachedir / f"{name}.git",
                outdir,
                args.offline,
                cutoff,
                combined,
                api_cache_path,
                api_cache,
                now,
            )
        except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
            print(f"WARNING: failed to process {url}: {e}", file=sys.stderr)
            failures += 1

    if not meta["buckets"]:
        print("ERROR: no buckets could be processed", file=sys.stderr)
        return 1
    if failures:
        print("ERROR: refusing to write partial combined CSVs", file=sys.stderr)
        return 1

    write_csv(
        outdir / "archived.csv",
        ["bucket", "name", "archived_date"],
        combined["archived"],
        date_column=2,
    )
    write_csv(
        outdir / "broken_home.csv",
        ["bucket", "name", "broken_home"],
        combined["broken_home"],
        date_column=2,
    )
    write_csv(
        outdir / "inactive.csv",
        ["bucket", "name", "latest_update"],
        combined["inactive"],
        date_column=2,
    )
    write_csv(
        outdir / "deprecated.csv",
        ["bucket", "name", "deprecated_date", "description"],
        combined["deprecated"],
        date_column=2,
    )

    meta["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (outdir / "scoop-manifests.meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Done: {len(meta['buckets'])} buckets processed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
