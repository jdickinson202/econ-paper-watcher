# econ-paper-watcher

Once a day this checks the listing pages of economics working-paper series that are hard to follow any other way: most
have no RSS feed of their own with only papers on it. It began with series whose papers don't reach NEP, and it also
covers the series that [EconGoat](https://econgoat.com/working-papers/) can't find through Crossref or EconStor. Papers
it hasn't seen before go into one Atom feed, so you can follow them in the same reader as NEP and the publishers' own
feeds.

- **Feed:** `docs/feed.xml`, published by GitHub Pages at `https://<owner>.github.io/econ-paper-watcher/feed.xml`
- **Status page:** `docs/index.html` lists each source as watched, failing or not watched, with the reason.

## How it works

1. `watch.py` reads the recipes in `sources.toml`.
2. For each enabled source it checks the site's robots.txt and fetches the listing page once. Requests identify themselves
   as `econ-paper-watcher`. The links the recipe accepts count as that source's papers.
3. Links that are not in `state/seen.json` become feed entries, dated the day the watcher first saw them. The first
   check of a newly added source records what is already there without adding entries, so the feed never fills up
   with a back catalogue.
4. A source that fails three runs in a row (blocked, page gone, or the recipe finds nothing) gets one warning entry
   in the feed.
5. More than 25 new links from one source at once become a single entry. That usually means a redesign or a whole
   conference program going up.

A GitHub Actions workflow (`.github/workflows/watch.yml`) runs this every morning and commits the updated state and
feed.

## Recipe fields (`sources.toml`)

| Field | Meaning |
| --- | --- |
| `name`, `series` | Shown in the feed and on the status page. |
| `url` | The listing page. `{year}` is replaced by the current year. |
| `links` | Regular expression a paper's link must match (tested against the full URL). |
| `skip` | Regular expression for links to ignore even when `links` matches. |
| `scope` | CSS selector for the part of the page that holds the list. |
| `item` | CSS selector for one paper's block. Each block gives one paper: its first link that passes `links`. |
| `title` | With `item`: CSS selector for the title inside the block. Otherwise the link text is the title, or the nearest heading when the link only says "PDF" or "Read more". |
| `item_text` | With `item`: `true` uses the block's whole text as the title, for pages that put the title in loose text beside a "Full text" link. |
| `title_match` | Regular expression whose first group is kept as the title, such as `'“(.+?)”'` for a title in curly quotes. A title it doesn't match is kept whole. |
| `min_title` | Shortest title accepted (default 12 characters, or 10 for `feed` and `json`). |
| `strip_query` | `true` drops the query string from links before comparing them. |
| `rewrite` | List of `[pattern, replacement]` pairs (regular expressions) applied to each link before anything else, for sites whose data holds links in an odd form. |
| `max_new` | Overrides the 25-link threshold for a single bulk entry. |
| `allow_empty` | `true` for a page that can legitimately list nothing, such as one that shows only the current year's papers. Otherwise an empty result counts as a failure. |
| `kind` | `"html"` (default), `"feed"` for an RSS or Atom feed, or `"json"` with `json_items` (dotted path to the list), `json_title` (title field, such as `"title"` or `"title.rendered"`) and `json_link` (link field, or a template such as `"https://example.org/wp?id={id}"`). |
| `enabled = false` | A source the watcher can't read. Give a `reason`, and optionally an `alert` link to the publisher's email alerts. Both appear on the status page. |
| `note` | Free text for whoever maintains the recipe. |

## Fixing a source

```bash
python watch.py --check "Bank of Israel"
```

That prints how many papers the recipe finds and the first few titles and links, without touching the state or the feed.
Edit the recipe in `sources.toml` until the titles look right, then commit. `--check all` runs every recipe.

## Running it yourself

```bash
pip install -r requirements.txt
python watch.py
```

## Manners

The watcher makes one request per listing page per day and follows robots.txt. It doesn't use a headless browser or
disguise itself as one. Sites that refuse automated visitors are left out and listed on the status page instead.
