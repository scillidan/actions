#!/usr/bin/env python3
"""Re-publish a handful of feeds as clean Atom feeds.

Most sources need their own rules (title cleanup, link rewriting, body
extraction, keyword filters), so each source has a small handler below.

The generated feed file doubles as the state: entries already present in it
are never processed again. That keeps request counts low and guarantees a
fast-scrolling source (typst packages, CTAN announcements) loses nothing
between runs.
"""

import calendar
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple
from urllib.parse import quote
from xml.etree import ElementTree as ET

import feedparser

ATOM = "http://www.w3.org/2005/Atom"
ET.register_namespace("", ATOM)

HERE = os.path.dirname(os.path.abspath(__file__))
FEED_DIR = os.path.join(HERE, "feeds")
MAX_ENTRIES = 300
WORKERS = 6
TIMEOUT = 45
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}

CTAN_EXCLUDE = (
    "chemical",
    "letter",
    "math",
    "physics",
    "resume",
    "thesis",
    "university",
    "unofficial"
)
TYPST_FIRST_RELEASE = {"1.0.0", "0.1.0", "0.0.1"}

YAZI_LINK = re.compile(r'^\s*<a href="(https?://[^"]+)">([^<]+)</a>\s*-\s*(.+)')
NEOVIM_LINK = re.compile(r"^\s*-\s*\[([^\]]+)\]\((https?://[^)]+)\)\s*-\s*(.+)")


class Feed(NamedTuple):
    name: str
    url: str
    title: str
    handler: object


def iso(parsed):
    dt = datetime.datetime.fromtimestamp(
        calendar.timegm(parsed), tz=datetime.timezone.utc
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry_time(e):
    parsed = e.get("updated_parsed") or e.get("published_parsed")
    return iso(parsed) if parsed else now_iso()


def get(url, headers=None, attempts=3):
    last = None
    for attempt in range(attempts):
        req = urllib.request.Request(url, headers={**UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2 * (attempt + 1))
    print(f"Error: {url} ({last})", file=sys.stderr)
    return None


def get_many(urls):
    if not urls:
        return []
    # terminaltrove.com rejects aiohttp but accepts urllib, hence threads here.
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(get, urls))


def pre_text(e):
    """Commit/announcement bodies arrive wrapped in a <pre> block."""
    raw = e.content[0].value if e.get("content") else e.get("summary", "")
    match = re.search(r"<pre[^>]*>(.*?)</pre>", raw, re.S)
    text = match.group(1) if match else raw
    return html.unescape(re.sub(r"<br\s*/?>", "\n", text))


def is_add(title):
    return bool(re.match(r"(?i)^add\b", title.strip()))


def clean_title(title):
    """'Add `foo/bar.nvim` (#123)' -> 'foo/bar.nvim'."""
    text = re.sub(r"(?i)^add\s+", "", title.strip())
    text = re.sub(r"\s*\(#\d+\)\s*$", "", text)
    return text.strip().strip("`").strip()


def section(text, marker):
    """Text after `marker` (a regex), until the next bullet/footer."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        found = re.search(marker, line, re.I)
        if not found:
            continue
        rest = line[found.end() :].lstrip(" :")
        parts = [rest] if rest else []
        for nxt in lines[i + 1 :]:
            stripped = nxt.strip()
            if not stripped:
                if parts:
                    break
                continue
            low = stripped.lower()
            if (
                stripped.startswith("* ")
                or stripped.startswith("---------")
                or stripped.startswith("<!--")
                or low.startswith("co-authored-by")
            ):
                break
            parts.append(stripped)
        return " ".join(p for p in parts if p).strip()
    return ""


def url_after(text, marker):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        found = re.search(re.escape(marker), line, re.I)
        if not found:
            continue
        for candidate in [line[found.end() :]] + lines[i + 1 : i + 6]:
            url = re.search(r"https?://[^\s<>\"']+", candidate)
            if url:
                return url.group(0).rstrip(".,)")
        break
    return None


def first_paragraph(text):
    """Fallback body for commits that skip the issue template."""
    lines = text.splitlines()[1:]
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("* ") or stripped.startswith("<!--"):
            return ""
        parts = [stripped]
        for nxt in lines[i + 1 :]:
            nxt = nxt.strip()
            if (
                not nxt
                or nxt.startswith("* ")
                or nxt.startswith("---------")
                or nxt.startswith("<!--")
                or nxt.lower().startswith("co-authored-by")
            ):
                break
            parts.append(nxt)
        return " ".join(parts)
    return ""


def first_github(text):
    for match in re.finditer(
        r"https://github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+", text
    ):
        if "ratatui/awesome-ratatui" not in match.group(0):
            return match.group(0)
    return None


def build(e, key, title, link, summary):
    return {
        "id": key,
        "title": title,
        "link": link,
        "summary": summary,
        "updated": entry_time(e),
    }


def source_known(known, link):
    return link in known or any(k.startswith(link + "#") for k in known)


# --- per-source handlers -------------------------------------------------


def project_url(page):
    """The tool's own repo link, taken from the page's JSON-LD metadata."""
    same_as = re.search(r'"sameAs":\[(.*?)\]', page or "")
    urls = re.findall(r'"(https?://[^"]+)"', same_as.group(1)) if same_as else []
    if not urls:
        help_url = re.search(r'"softwareHelp":\{[^}]*"url":"([^"]+)"', page or "")
        urls = [help_url.group(1)] if help_url else []
    github = [u for u in urls if "github.com" in u]
    return (github or urls or [None])[0]


def handle_terminal_trove(feed, known):
    todo = [e for e in feed.entries if not source_known(known, e.link)]
    pages = get_many([e.link for e in todo])
    out = []
    for e, page in zip(todo, pages):
        out.append(
            build(
                e,
                e.link,
                e.title,
                project_url(page) or e.link,
                e.get("summary", "").strip(),
            )
        )
    return out


def handle_fdroid(feed, known):
    return [
        build(e, e.link, e.title, e.link, e.get("summary", "").strip())
        for e in feed.entries
        if not source_known(known, e.link)
    ]


def handle_awesome_ratatui(feed, known):
    out = []
    for e in feed.entries:
        if not is_add(e.title) or source_known(known, e.link):
            continue
        text = pre_text(e)
        out.append(
            build(
                e,
                e.link,
                clean_title(e.title),
                url_after(text, "Link to your project") or first_github(text) or e.link,
                section(text, r"Description(?: \(optional\))?")
                or first_paragraph(text),
            )
        )
    return out


def github_added_lines(commit_url):
    match = re.match(
        r"https://github\.com/([^/]+)/([^/]+)/commit/([0-9a-fA-F]+)", commit_url
    )
    if not match:
        return []
    api = "https://api.github.com/repos/{}/{}/commits/{}".format(*match.groups())
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = get(api, headers)
    if not body:
        return None
    try:
        files = json.loads(body).get("files") or []
    except ValueError:
        return None
    return [
        line[1:]
        for f in files
        for line in (f.get("patch") or "").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def handle_add_commits(feed, known, pattern, extract):
    todo = [
        e for e in feed.entries if is_add(e.title) and not source_known(known, e.link)
    ]
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        patches = list(pool.map(github_added_lines, [e.link for e in todo]))
    out = []
    for e, lines in zip(todo, patches):
        if lines is None:
            # API failure: leave the commit unprocessed so the next run retries it.
            continue
        found = []
        for line in lines:
            match = pattern.search(line)
            if match:
                found.append([part.strip().strip("`") for part in extract(match)])
        if not found:
            # Keep the commit itself rather than silently dropping the addition.
            out.append(build(e, e.link, clean_title(e.title), e.link, ""))
            continue
        seen = set()
        for title, url, desc in found:
            if url in seen:
                continue
            seen.add(url)
            out.append(build(e, f"{e.link}#{url}", title, url, desc))
    return out


def handle_awesome_yazi(feed, known):
    # Patch line: <a href="url">name</a> - description
    return handle_add_commits(
        feed, known, YAZI_LINK, lambda m: (m.group(2), m.group(1), m.group(3))
    )


def handle_awesome_neovim(feed, known):
    # Patch line: - [name](url) - description
    return handle_add_commits(feed, known, NEOVIM_LINK, lambda m: m.groups())


def handle_ctan(feed, known):
    todo = []
    for e in feed.entries:
        if not e.title.lower().startswith("new on ctan:"):
            continue
        if source_known(known, e.link):
            continue
        text = pre_text(e)
        description = re.search(r"Summary description:\s*(.+)", text)
        summary = description.group(1).strip() if description else ""
        blob = f"{e.title} {summary}".lower()
        if any(word in blob for word in CTAN_EXCLUDE):
            continue
        todo.append((e, summary))

    pages = get_many([e.link for e, _ in todo])
    out = []
    for (e, summary), page in zip(todo, pages):
        title = re.sub(r"(?i)^new on ctan:\s*", "", e.title).strip()
        pkg = re.search(r"https://ctan\.org/pkg/[A-Za-z0-9._\-]+", page or "")
        link = pkg.group(0) if pkg else f"https://ctan.org/pkg/{quote(title)}"
        out.append(build(e, e.link, title, link, summary))
    return out


def handle_typst_packages(feed, known):
    out = []
    for e in feed.entries:
        if source_known(known, e.link):
            continue
        match = re.match(
            r"^\s*([^:]+):\s*([0-9][0-9A-Za-z.\-]*)\s*(?:\(#\d+\))?\s*$", e.title
        )
        if not match:
            continue
        pkg, version = match.group(1).strip(), match.group(2)
        if version not in TYPST_FIRST_RELEASE:
            continue
        url = f"https://typst.app/universe/search/?q={quote(pkg, safe='')}"
        out.append(build(e, e.link, pkg, url, ""))
    return out


FEEDS = [
    Feed(
        "terminal-trove",
        "https://terminaltrove.com/new.xml",
        "Terminal Trove: New Tools",
        handle_terminal_trove,
    ),
    Feed(
        "fdroid-new-apps",
        "https://newapps.f-droid.othing.xyz/new_apps.en-US.xml",
        "F-Droid: New Apps",
        handle_fdroid,
    ),
    Feed(
        "awesome-ratatui",
        "https://github.com/ratatui/awesome-ratatui/commits.atom",
        "awesome-ratatui: New Additions",
        handle_awesome_ratatui,
    ),
    Feed(
        "awesome-yazi",
        "https://github.com/AnirudhG07/awesome-yazi/commits.atom",
        "awesome-yazi: New Plugins",
        handle_awesome_yazi,
    ),
    Feed(
        "awesome-neovim",
        "https://github.com/rockerBOO/awesome-neovim/commits.atom",
        "awesome-neovim: New Plugins",
        handle_awesome_neovim,
    ),
    Feed(
        "ctan-ann",
        "https://www.ctan.org/ctan-ann/atom",
        "CTAN: New Packages",
        handle_ctan,
    ),
    Feed(
        "typst-packages",
        "https://github.com/typst/packages/commits.atom",
        "Typst Universe: New Packages",
        handle_typst_packages,
    ),
]


# --- atom output ---------------------------------------------------------


def load_published(name):
    path = os.path.join(FEED_DIR, name + ".xml")
    if not os.path.exists(path):
        return []
    root = ET.parse(path).getroot()

    def text(node, tag):
        found = node.find(f"{{{ATOM}}}{tag}")
        return (found.text or "") if found is not None else ""

    out = []
    for node in root.findall(f"{{{ATOM}}}entry"):
        link = node.find(f"{{{ATOM}}}link")
        out.append(
            {
                "id": text(node, "id"),
                "title": text(node, "title"),
                "link": link.get("href", "") if link is not None else "",
                "summary": text(node, "summary"),
                "updated": text(node, "updated") or now_iso(),
            }
        )
    return out


def write_feed(name, title, entries):
    root = ET.Element(f"{{{ATOM}}}feed")
    ET.SubElement(root, f"{{{ATOM}}}title").text = title
    ET.SubElement(root, f"{{{ATOM}}}id").text = f"urn:rss-preread:{name}"
    ET.SubElement(root, f"{{{ATOM}}}updated").text = (
        entries[0]["updated"] if entries else now_iso()
    )
    ET.SubElement(
        root,
        f"{{{ATOM}}}link",
        {
            "rel": "self",
            "href": "https://raw.githubusercontent.com/scillidan/actions/main/"
            f"rss-preread/feeds/{name}.xml",
        },
    )
    for e in entries:
        node = ET.SubElement(root, f"{{{ATOM}}}entry")
        ET.SubElement(node, f"{{{ATOM}}}id").text = e["id"]
        ET.SubElement(node, f"{{{ATOM}}}title").text = e["title"]
        ET.SubElement(node, f"{{{ATOM}}}link", {"href": e["link"]})
        ET.SubElement(node, f"{{{ATOM}}}updated").text = e["updated"]
        if e["summary"]:
            summary = ET.SubElement(node, f"{{{ATOM}}}summary", {"type": "html"})
            summary.text = e["summary"]

    os.makedirs(FEED_DIR, exist_ok=True)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    path = os.path.join(FEED_DIR, name + ".xml")
    tmp = path + ".tmp"
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, path)


def merge(new, existing):
    seen = set()
    merged = []
    for e in new + existing:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        merged.append(e)
    merged.sort(key=lambda e: e["updated"], reverse=True)
    return merged[:MAX_ENTRIES]


def run():
    for spec in FEEDS:
        body = get(spec.url)
        if not body:
            continue
        feed = feedparser.parse(body)
        existing = load_published(spec.name)
        new = spec.handler(feed, {e["id"] for e in existing})
        merged = merge(new, existing)
        write_feed(spec.name, spec.title, merged)
        print(f"[{spec.name}] +{len(new)} new (total {len(merged)})")


# --- self check ----------------------------------------------------------


def selftest():
    assert clean_title("Add ff (#550)") == "ff"
    assert clean_title("Add `a/b.nvim` (#1)") == "a/b.nvim"
    assert is_add("Add x") and is_add("add x") and not is_add("Added x")

    assert (
        project_url(
            '"sameAs":["https://codeberg.org/w/b","https://codeberg.org/w/b"],'
            '"softwareHelp":{"@type":"CreativeWork","url":"https://codeberg.org/w/b"}'
        )
        == "https://codeberg.org/w/b"
    )
    assert (
        project_url('"sameAs":["https://example.org/x","https://github.com/w/x"]')
        == "https://github.com/w/x"
    )

    text = (
        "Add whycodes (#546)\n\n* Link to your project (GitHub/crates.io):\n"
        "https://github.com/w/c\n\n* Description (optional): A tool.\nWrapped line.\n\n"
        "* Screenshot or gif (strongly recommended):\n"
    )
    assert url_after(text, "Link to your project") == "https://github.com/w/c"
    assert section(text, r"Description(?: \(optional\))?") == "A tool. Wrapped line."
    assert (
        section("* Description: Plain.\nMore.\n\n* Screenshot:", "Description")
        == "Plain. More."
    )
    assert first_paragraph(
        "Add x (#1)\n\nAdds [cct](https://github.com/v/c) to AI."
    ) == ("Adds [cct](https://github.com/v/c) to AI.")
    assert (
        first_paragraph("Add x (#1)\n\n* Link to your project:\nhttps://github.com/w/c")
        == ""
    )
    assert first_github(
        "see https://github.com/ratatui/awesome-ratatui/x and https://github.com/v/c"
    ) == ("https://github.com/v/c")

    yazi = '<a href="https://github.com/t/repo-status.yazi">repo-status.yazi</a> - Show status.'
    m = YAZI_LINK.search(yazi)
    assert m.groups() == (
        "https://github.com/t/repo-status.yazi",
        "repo-status.yazi",
        "Show status.",
    )
    m = YAZI_LINK.search(
        '  <a href="https://tangled.org/x/hxjump.yazi">hxjump.yazi</a> - Jump around'
    )
    assert m and m.group(1) == "https://tangled.org/x/hxjump.yazi"

    nvim = (
        "- [L/macscroll.nvim](https://github.com/L/macscroll.nvim) - macOS scrolling."
    )
    m = NEOVIM_LINK.search(nvim)
    assert m.groups() == (
        "L/macscroll.nvim",
        "https://github.com/L/macscroll.nvim",
        "macOS scrolling.",
    )

    ctan = "Version: 2.0.1\n\nSummary description: Floral margins\n\nAnnouncement text:\n..."
    assert re.search(r"Summary description:\s*(.+)", ctan).group(1) == "Floral margins"

    typst = re.match(
        r"^\s*([^:]+):\s*([0-9][0-9A-Za-z.\-]*)\s*(?:\(#\d+\))?\s*$",
        "ling-handout:0.1.0 (#6294)",
    )
    assert typst.groups() == ("ling-handout", "0.1.0")

    assert (
        merge(
            [{"id": "a", "updated": "2026-01-02T00:00:00Z"}],
            [
                {"id": "a", "updated": "2026-01-01T00:00:00Z"},
                {"id": "b", "updated": "2026-01-03T00:00:00Z"},
            ],
        )[0]["id"]
        == "b"
    )

    # Round trip must not escape HTML twice.
    import shutil
    import tempfile

    global FEED_DIR
    original, tmp = FEED_DIR, tempfile.mkdtemp()
    try:
        FEED_DIR = tmp
        write_feed(
            "t",
            "t",
            [
                {
                    "id": "1",
                    "title": "x",
                    "link": "u",
                    "summary": "A & B <c>",
                    "updated": "2026-01-01T00:00:00Z",
                }
            ],
        )
        loaded = load_published("t")
        assert loaded[0]["summary"] == "A & B <c>"
        assert loaded[0]["link"] == "u"
    finally:
        FEED_DIR = original
        shutil.rmtree(tmp)
    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
