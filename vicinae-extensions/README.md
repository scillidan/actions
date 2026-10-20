# vicinae-extensions

Track new community extensions on [vicinae.com/extensions](https://www.vicinae.com/extensions).

The site has no RSS feed, but it is a Next.js app backed by a public JSON API
that the page itself calls:

```
GET https://api.vicinae.com/v1/store/list?limit=500
-> { "extensions": [...], "pagination": {...} }
```

Each extension carries `id`, `name`, `title`, `description`, `author`,
`createdAt`, `categories`, `downloadUrl`, `sourceUrl`, etc. The script diffs
that list against `state.json` and emits:

- `vicinae-updates.txt` — append-only log, one line per new extension:
  `<createdAt> <url> <title> - <author>`
- `vicinae-feed.xml` — RSS 2.0, rebuilt from the full state whenever new
  extensions appear; categories are in `<category>`

Existing extensions are backfilled into the state on the first run (no
"new" spam), so only extensions published afterwards show up as new.

## Usage

```bash
pip install -r requirements.txt
python vicinae_extensions.py
```

Options: `--limit N` (max RSS items, default 100), plus `--state/--txt/--rss/
--feed-title` to override file paths. `NEW_EXTENSIONS` is written to
`$GITHUB_ENV` when running under Actions.

## Files

| file | role |
|------|------|
| `state.json` | known extensions keyed by store id (script-maintained) |
| `vicinae-updates.txt`, `vicinae-feed.xml` | generated outputs |

Runs every 6 hours via `.github/workflows/vicinae-extensions.yml`.

## Subscribing

The repo is public, so the committed feed is directly readable:

```
https://raw.githubusercontent.com/scillidan/actions/main/vicinae-extensions/vicinae-feed.xml
```

Subscribe to that URL in your reader. Raw content is CDN-cached for a few
minutes, which is irrelevant for a feed updated every 6 hours.
