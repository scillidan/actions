#!/usr/bin/env python3
"""Generate <bucket>_added.csv / <bucket>_deprecated.csv from buckets.txt.

Input:
  - buckets.txt: one scoop bucket git repo URL per line

Output (one set per bucket):
  - <bucket>_added.csv:       manifests currently in bucket/
                              (name,date,description)
  - <bucket>_deprecated.csv:  manifests currently in deprecated/
                              (name,date)
  - scoop-manifests.meta.json: run metadata and per-bucket counts

Dates are the commit date (YYYY-MM-DD) of a manifest's first appearance in
git history; rows are sorted newest first. Upstream-friendly by design: each
bucket is kept as a cached bare clone (.cache/<bucket>.git) — one clone on
first run, then one small incremental fetch per run. A failed fetch falls
back to the cached clone instead of failing the run. No GitHub API or token
is needed for public repos.

Use --offline to skip fetching (e.g. on a machine without network access to
the git remote); buckets without a cached clone are skipped with a warning.
"""

import argparse
import csv
import json
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent


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


def current_manifests(repo: Path, prefix: str) -> list[str]:
    out = git(repo, "ls-tree", "-r", "--name-only", "HEAD", "--", f"{prefix}/")
    return [
        line.removeprefix(f"{prefix}/")[:-5]
        for line in out.splitlines()
        if line.lower().endswith(".json")
    ]


def descriptions(repo: Path, prefix: str) -> dict[str, str]:
    """manifest name -> description field, read from the current tree."""
    proc = subprocess.Popen(
        ["git", "-C", str(repo), "archive", "--format=tar", "HEAD", "--", f"{prefix}/"],
        stdout=subprocess.PIPE,
    )
    desc: dict[str, str] = {}
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
                value = data.get("description", "")
                desc[name[:-5]] = value if isinstance(value, str) else ""
            except (ValueError, UnicodeDecodeError):
                continue
    proc.wait()
    return desc


def write_csv(path: Path, header: list[str], rows: list[tuple]) -> None:
    rows = sorted(rows)  # name asc (stable within equal dates)
    rows.sort(key=lambda r: r[1], reverse=True)  # date desc
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def process_bucket(url: str, cache: Path, outdir: Path, offline: bool) -> dict:
    name = bucket_name(url)
    ensure_repo(url, cache, offline)

    add_dates = first_add_dates(cache, "bucket")
    dep_dates = first_add_dates(cache, "deprecated")
    added = current_manifests(cache, "bucket")
    deprecated = current_manifests(cache, "deprecated")
    descs = descriptions(cache, "bucket")

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

    added_rows = [(m, date_of(add_dates, m), descs.get(m, "")) for m in added]
    dep_rows = [(m, date_of(dep_dates, m)) for m in deprecated]

    write_csv(outdir / f"{name}_added.csv", ["name", "date", "description"], added_rows)
    write_csv(outdir / f"{name}_deprecated.csv", ["name", "date"], dep_rows)

    return {"url": url, "added": len(added_rows), "deprecated": len(dep_rows)}


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
    parser.add_argument("--offline", action="store_true", help="skip git fetch/clone")
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
    cachedir = Path(args.cachedir)
    meta = {"generated": "", "buckets": {}}
    failures = 0
    for url in urls:
        name = bucket_name(url)
        try:
            print(f"[{name}] processing {url} ...", file=sys.stderr)
            meta["buckets"][name] = process_bucket(
                url, cachedir / f"{name}.git", outdir, args.offline
            )
        except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
            print(f"WARNING: failed to process {url}: {e}", file=sys.stderr)
            failures += 1

    if not meta["buckets"]:
        print("ERROR: no buckets could be processed", file=sys.stderr)
        return 1

    meta["generated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (outdir / "scoop-manifests.meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Done: {len(meta['buckets'])} buckets processed, {failures} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
