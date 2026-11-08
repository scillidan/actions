# rss-preread

Re-publishes a few feeds that need per-source cleanup as plain Atom feeds.
Each source has its own handler in `rss_preread.py` (title cleanup, link
rewriting, body extraction, keyword filters, version filters).

The generated feed file **is** the state: entries already in it are never
processed again, so a fast-scrolling source (typst packages, CTAN
announcements) loses nothing between runs. To re-process an entry, delete it
from the feed file.

## Subscribe

| Feed | Source |
| --- | --- |
| https://raw.githubusercontent.com/scillidan/actions/main/rss-preread/feeds/terminal-trove.xml | https://terminaltrove.com/new.xml |
| https://raw.githubusercontent.com/scillidan/actions/main/rss-preread/feeds/fdroid-new-apps.xml | https://newapps.f-droid.othing.xyz/new_apps.en-US.xml |
| https://raw.githubusercontent.com/scillidan/actions/main/rss-preread/feeds/awesome-ratatui.xml | https://github.com/ratatui/awesome-ratatui/commits.atom |
| https://raw.githubusercontent.com/scillidan/actions/main/rss-preread/feeds/awesome-yazi.xml | https://github.com/AnirudhG07/awesome-yazi/commits.atom |
| https://raw.githubusercontent.com/scillidan/actions/main/rss-preread/feeds/awesome-neovim.xml | https://github.com/rockerBOO/awesome-neovim/commits.atom |
| https://raw.githubusercontent.com/scillidan/actions/main/rss-preread/feeds/ctan-ann.xml | https://www.ctan.org/ctan-ann/atom |
| https://raw.githubusercontent.com/scillidan/actions/main/rss-preread/feeds/typst-packages.xml | https://github.com/typst/packages/commits.atom |

If a reader rejects the `text/plain` content type of raw.githubusercontent.com,
use `https://cdn.jsdelivr.net/gh/scillidan/actions@main/rss-preread/feeds/<name>.xml`
instead.

## Rules

| Feed | Kept | Title | Link | Body |
| --- | --- | --- | --- | --- |
| terminal-trove | all | unchanged | tool repo from the page's JSON-LD (GitHub/Codeberg) | short summary |
| fdroid-new-apps | all | unchanged | package page | short summary |
| awesome-ratatui | `Add ...` commits | `Add xxx (#n)` → `xxx` | "Link to your project" URL | "Description" section |
| awesome-yazi | `Add ...` commits | link text | URL of the added line | text after ` - ` |
| awesome-neovim | `Add ...` commits | link text | URL of the added line | text after ` - ` |
| ctan-ann | `New on CTAN:` | package name | `https://ctan.org/pkg/...` | `Summary description:` |
| typst-packages | versions `1.0.0`, `0.1.0`, `0.0.1` | package name | Typst Universe search | none |

CTAN entries whose title or summary contains `thesis`, `university`, `math`,
`chemical`, `physics`, `letter` or `resume` are dropped.

## Local run

```sh
pip install -r requirements.txt
python rss_preread.py --selftest
GITHUB_TOKEN=$(gh auth token) python rss_preread.py
```

`GITHUB_TOKEN` raises the GitHub API rate limit used to read commit patches.
